import os
from abc import ABCMeta, abstractmethod
import hashlib
import shutil

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, ConcatDataset
from transformers import AutoTokenizer, AutoModelForSequenceClassification, BertConfig
from tqdm import tqdm
import polars as pl
import h5py
import pyfaidx
import pyBigWig

from ..finetune import HFClassifierModel, LoRAModule
from ..utils import onehot_to_chars, one_hot_encode, NoModule


class ChromatinEndToEndDataset(Dataset):
    _elements_dtypes = {
        "chr": pl.Utf8,
        "input_start": pl.UInt32,
        "input_end": pl.UInt32,
        "elem_start": pl.UInt32,
        "elem_end": pl.UInt32,
    }

    def __init__(self, genome_fa, bigwig, elements_tsv, chroms, crop, downsample_ratio=None, cache_dir=None):
        super().__init__()

        self.crop = crop

        self.elements_df_all = self._load_elements(elements_tsv, chroms)

        if cache_dir is not None:
            os.makedirs(cache_dir, exist_ok=True)
            
            bw_path_abs = os.path.abspath(bigwig)
            bw_path_hash = hashlib.sha256(bw_path_abs.encode('utf-8')).hexdigest()
            bw_cache_path = os.path.join(cache_dir, bw_path_hash + ".bw")
            self._copy_if_not_exists(bigwig, bw_cache_path)
            bigwig = bw_cache_path

            fa_path_abs = os.path.abspath(genome_fa)
            fa_idx_path_abs = fa_path_abs + ".fai"
            fa_path_hash = hashlib.sha256(fa_path_abs.encode('utf-8')).hexdigest()
            fa_cache_path = os.path.join(cache_dir, fa_path_hash + ".fa")
            fa_idx_cache_path = fa_cache_path + ".fai"
            self._copy_if_not_exists(genome_fa, fa_cache_path)
            genome_fa = fa_cache_path
            try:
                self._copy_if_not_exists(fa_idx_path_abs, fa_idx_cache_path)
            except FileNotFoundError:
                pass

        self.genome_fa = genome_fa
        fa = pyfaidx.Fasta(self.genome_fa) # Build index if needed
        fa.close()

        self.bw = bigwig

        self.downsample_ratio = downsample_ratio
        if downsample_ratio is None:
            self.elements_df = self.elements_df_all

    @classmethod
    def _load_elements(cls, elements_file, chroms):
        df = pl.scan_csv(elements_file, separator="\t", quote_char=None, dtypes=cls._elements_dtypes)
        
        if chroms is not None:
                df = df.filter(pl.col("chr").is_in(chroms))

        df = df.collect()

        return df

    @staticmethod
    def _copy_if_not_exists(src, dst):
        try:
            with open(dst, "xb") as f, open(src, "rb") as sf:
                shutil.copyfileobj(sf, f)
        except FileExistsError:
            pass

    def set_epoch(self, epoch):
        if self.downsample_ratio is None:
            return

        offset = epoch % self.downsample_ratio
        self.elements_df = self.elements_df_all.take_every(n=self.downsample_ratio, offset=offset)
    
    def __len__(self):
        return self.elements_df.height
    
    def __getitem__(self, idx):
        chrom, start, end, elem_start, elem_end, _, _ = self.elements_df.row(idx)

        seq = np.zeros((end - start, 4), dtype=np.int8)

        fa = pyfaidx.Fasta(self.genome_fa, one_based_attributes=False)

        sequence_data = fa[chrom][max(0, start):end]
        sequence = sequence_data.seq.upper()
        start_adj = sequence_data.start
        end_adj = sequence_data.end

        a = start_adj - start
        b = end_adj - start
        # print(peak_start, start_adj) ####
        # print(a,b) ####
        seq[a:b,:] = one_hot_encode(sequence)

        fa.close()

        out_start = start + self.crop
        out_end = end - self.crop
        out_start_adj = max(out_start, start_adj)
        out_end_adj = min(out_end, end_adj)

        c = out_start_adj - out_start
        d = out_end_adj - out_start

        signal = np.zeros(out_end - out_start, dtype=np.float32)

        bw = pyBigWig.open(self.bw)
        track = bw.values(chrom, out_start_adj, out_end_adj, numpy=True)
        signal[c:d] = np.nan_to_num(track)
        bw.close()

        return torch.from_numpy(seq), torch.from_numpy(signal)


def log1pMSELoss(log_predicted_counts, true_counts):
    log_true = torch.log(true_counts+1)
    return torch.mean(torch.square(log_true - log_predicted_counts), dim=-1)


def pearson_correlation(a, b):
    a = a - torch.mean(a)
    b = b - torch.mean(b)

    var_a = torch.sum(a ** 2)
    var_b = torch.sum(b ** 2)
    cov = torch.sum(a * b)

    r = cov / torch.sqrt(var_a * var_b)
    r = torch.nan_to_num(r)

    return r.item()

def counts_pearson(log_preds, targets):
    log_targets = torch.log(targets + 1)

    r = pearson_correlation(log_preds, log_targets)

    return r


def counts_spearman(log_preds, targets):
    log_targets = torch.log(targets + 1)

    preds_rank = log_preds.argsort().argsort().float()
    targets_rank = log_targets.argsort().argsort().float()

    r = pearson_correlation(preds_rank, targets_rank)

    return r
    

def train_finetuned_chromatin_model(train_pos_dataset, train_neg_dataset, val_pos_dataset, val_neg_dataset, model, 
                                    num_epochs, out_dir, batch_size, lr, wd, accumulate,
                                    num_workers, prefetch_factor, device, progress_bar=False, resume_from=None):

    val_pos_dataloader = DataLoader(val_pos_dataset, batch_size=batch_size, num_workers=num_workers, 
                                pin_memory=True, prefetch_factor=prefetch_factor, persistent_workers=True)
    val_neg_dataloader = DataLoader(val_neg_dataset, batch_size=batch_size, num_workers=num_workers,
                                pin_memory=True, prefetch_factor=prefetch_factor, persistent_workers=True)

    os.makedirs(out_dir, exist_ok=True)
    log_file = os.path.join(out_dir, "train.log")
    log_cols = ["epoch", "val_loss", "val_pearson_all", "val_spearman_all", "val_pearson_peaks", "val_spearman_peaks"]

    if resume_from is not None:
        # start_epoch = int(resume_from.split("_")[-1].split(".")[0]) + 1
        resume_checkpoint_path = os.path.join(out_dir, f"checkpoint_{resume_from}.pt")
        start_epoch = resume_from + 1
        checkpoint_resume = torch.load(resume_checkpoint_path)
        model.load_state_dict(checkpoint_resume, strict=False)
    else:
        start_epoch = 0

    with open(log_file, "a") as f:
        if resume_from is None:
            f.write("\t".join(log_cols) + "\n")
            f.flush()

        model.to(device)
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=wd)

        for epoch in range(start_epoch, num_epochs):
            model.train()
            train_pos_dataset.set_epoch(epoch)
            train_neg_dataset.set_epoch(epoch)
            train_dataset = ConcatDataset([train_pos_dataset, train_neg_dataset])
            train_dataloader = DataLoader(train_dataset, batch_size=batch_size, num_workers=num_workers, shuffle=True,
                                          pin_memory=True, prefetch_factor=prefetch_factor, persistent_workers=True)
            
            optimizer.zero_grad()
            for i, (seq, track) in enumerate(tqdm(train_dataloader, disable=(not progress_bar), desc="train", ncols=120)):
                # seq = seq.to(device)
                track = track.to(device)
                true_counts = track.sum(dim=1)
                
                try:
                    log1p_counts = model(seq).squeeze(1)
                    loss = log1pMSELoss(log1p_counts, true_counts) / accumulate
                    loss.backward()
                    
                except torch.cuda.OutOfMemoryError:
                    split_ind = len(seq) // 2
                    seq1, seq2 = seq[:split_ind], seq[split_ind:]
                    true_counts1, true_counts2 = true_counts[:split_ind], true_counts[split_ind:]

                    log1p_counts1 = model(seq1).squeeze(1)
                    loss1 = log1pMSELoss(log1p_counts1, true_counts1) / (accumulate * 2)
                    loss1.backward()

                    log1p_counts2 = model(seq2).squeeze(1)
                    loss2 = log1pMSELoss(log1p_counts2, true_counts2) / (accumulate * 2)
                    loss2.backward()

                if ((i + 1) % accumulate == 0):
                    optimizer.step()
                    optimizer.zero_grad()

            optimizer.step()
            
            val_loss = 0
            val_counts_pred = []
            val_counts_true = []
            model.eval()
            with torch.no_grad():
                for i, (seq, track) in enumerate(tqdm(val_pos_dataloader, disable=(not progress_bar), desc="val_pos", ncols=120)):
                    # seq = seq.to(device)
                    track = track.to(device)
                    true_counts = track.sum(dim=1)
                    
                    log1p_counts = model(seq).squeeze(1)
                    loss = log1pMSELoss(log1p_counts, true_counts)

                    val_loss += loss.item()
                    val_counts_pred.append(log1p_counts)
                    val_counts_true.append(true_counts)

                val_counts_pred_peaks = torch.cat(val_counts_pred, dim=0)
                val_counts_true_peaks = torch.cat(val_counts_true, dim=0)

                val_pearson_peaks = counts_pearson(val_counts_pred_peaks, val_counts_true_peaks)
                val_spearman_peaks = counts_spearman(val_counts_pred_peaks, val_counts_true_peaks)

                for i, (seq, track) in enumerate(tqdm(val_neg_dataloader, disable=(not progress_bar), desc="val_neg", ncols=120)):
                    # seq = seq.to(device)
                    track = track.to(device)
                    true_counts = track.sum(dim=1)
                    
                    log1p_counts = model(seq).squeeze(1)
                    loss = log1pMSELoss(log1p_counts, true_counts)

                    val_loss += loss.item()
                    val_counts_pred.append(log1p_counts)
                    val_counts_true.append(true_counts)

                val_loss /= (len(val_pos_dataloader) + len(val_neg_dataloader))
                val_counts_pred = torch.cat(val_counts_pred, dim=0)
                val_counts_true = torch.cat(val_counts_true, dim=0)

                val_pearson_all = counts_pearson(val_counts_pred, val_counts_true)
                val_spearman_all = counts_spearman(val_counts_pred, val_counts_true)

            print(f"Epoch {epoch}: val_loss={val_loss}, val_pearson_all={val_pearson_all}, val_spearman_all={val_spearman_all}, val_pearson_peaks={val_pearson_peaks}, val_spearman_peaks={val_spearman_peaks}")
            f.write(f"{epoch}\t{val_loss}\t{val_pearson_all}\t{val_spearman_all}\t{val_pearson_peaks}\t{val_spearman_peaks}\n")
            f.flush()

            checkpoint_path = os.path.join(out_dir, f"checkpoint_{epoch}.pt")
            torch.save(model.state_dict(), checkpoint_path)


# class ChromatinPredictionHead(torch.nn.Module):
#     def __init__(self, input_channels):
#         super().__init__()
#         self.linear = torch.nn.Linear(input_channels, 1)

#     def forward(self, embs):
#         x = self.linear(embs)
#         x = x.squeeze(-1)

#         return x

    
class DNABERT2LoRAModel(HFClassifierModel):
    def __init__(self, model_name, lora_rank, lora_alpha, lora_dropout, num_labels):
        model_name = f"zhihan1996/{model_name}"
        with NoModule("triton"):
            tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
            config = BertConfig.from_pretrained("zhihan1996/DNABERT-2-117M")
            config.num_labels = num_labels
            model = AutoModelForSequenceClassification.from_pretrained(model_name, trust_remote_code=True, config=config)
            model.bert = LoRAModule(model.bert, lora_rank, lora_alpha, lora_dropout)

        super().__init__(tokenizer, model)


class MistralDNALoRAModel(HFClassifierModel):
    def __init__(self, model_name, lora_rank, lora_alpha, lora_dropout, num_labels):
        model_name = f"RaphaelMourad/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForSequenceClassification.from_pretrained(model_name, trust_remote_code=True, num_labels=num_labels)
        model.config.pad_token_id = tokenizer.pad_token_id
        model.model = LoRAModule(model.model, lora_rank, lora_alpha, lora_dropout)
        
        super().__init__(tokenizer, model)


class GENALMLoRAModel(HFClassifierModel):
    def __init__(self, model_name, lora_rank, lora_alpha, lora_dropout, num_labels):
        model_name = f"AIRI-Institute/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForSequenceClassification.from_pretrained(model_name, trust_remote_code=True, num_labels=num_labels)
        model.bert = LoRAModule(model.bert, lora_rank, lora_alpha, lora_dropout)

        super().__init__(tokenizer, model)


class NucleotideTransformerLoRAModel(HFClassifierModel):
    def __init__(self, model_name, lora_rank, lora_alpha, lora_dropout, num_labels):
        model_name = f"InstaDeepAI/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForSequenceClassification.from_pretrained(model_name, trust_remote_code=True, num_labels=num_labels)
        model.esm = LoRAModule(model.esm, lora_rank, lora_alpha, lora_dropout)

        super().__init__(tokenizer, model)

    def _tokenize(self, seqs):
        seqs_str = onehot_to_chars(seqs)
        encoded = self.tokenizer(seqs_str, return_tensors="pt", padding=True)
        tokens = encoded["input_ids"]

        return tokens.to(self.device), None


class HyenaDNALoRAModel(HFClassifierModel):
    def __init__(self, model_name, lora_rank, lora_alpha, lora_dropout, num_labels):
        model_name = f"LongSafari/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True, padding_side="right")
        model = AutoModelForSequenceClassification.from_pretrained(model_name, trust_remote_code=True, num_labels=num_labels)
        model.hyena = LoRAModule(model.hyena, lora_rank, lora_alpha, lora_dropout)

        super().__init__(tokenizer, model)

    def _tokenize(self, seqs):
        seqs_str = onehot_to_chars(seqs)
        encoded = self.tokenizer(seqs_str, return_tensors="pt", padding=True)
        tokens = encoded["input_ids"]

        return tokens.to(self.device), None
    
    def forward(self, seqs):
        tokens, _ = self._tokenize(seqs)

        torch_outs = self.model(tokens)
        logits = torch_outs.logits

        return logits
