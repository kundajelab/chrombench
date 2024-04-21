from abc import ABCMeta, abstractmethod

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForMaskedLM, AutoModel, AutoModelForCausalLM, BertConfig
from scipy.stats import wilcoxon
from tqdm import tqdm
from ..utils import NoModule, onehot_to_chars
import polars as pl

class LikelihoodEvaluator(metaclass=ABCMeta):
    def __init__(self, tokenizer, model, batch_size, num_workers, device):
        self.tokenizer = tokenizer
        self.model = model
        self.model.to(device)
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.device = device
        
    @property
    @abstractmethod
    def start_token(self):
        pass

    @property
    @abstractmethod
    def end_token(self):
        pass
    
    @property
    def mask_token(self):
        return self.tokenizer.mask_token_id

    def tokenize(self, seqs):
        seqs_str = onehot_to_chars(seqs)
        encoded = self.tokenizer.batch_encode_plus(seqs_str, return_tensors="pt", padding=True)
        tokens = encoded["input_ids"]
        try:
            attention_mask = encoded["attention_mask"]
        except:
            attention_mask = None
        if self.start_token is not None:
            starts = torch.where(tokens == self.start_token)[1] + 1 
        else:
            starts = torch.tensor([0]*tokens.shape[0])
        if self.end_token is not None:
            ends = torch.where(tokens == self.end_token)[1]
        else:
            ends = attention_mask.sum(dim=1) 
        return tokens, starts, ends, attention_mask 

    def model_fwd(self, tokens, attention_mask):
        with torch.no_grad():
            try:
                torch_outs = self.model(
                    tokens,
                    attention_mask=attention_mask,
                    encoder_attention_mask=attention_mask
                )
            except:
                torch_outs = self.model(tokens)
            logits = torch_outs.logits.swapaxes(1, 2)
            lls = -F.cross_entropy(logits, tokens, reduction="none")
        return lls

    # @abstractmethod
    # def score(self, tokens, starts, ends, attention_mask):
    #     pass


    def evaluate(self, dataset, output_file, progress_bar=True):
        out_file_obj = open(output_file, "w")
        dataloader = DataLoader(dataset, batch_size=self.batch_size, shuffle=False, num_workers=self.num_workers)
        for seqs in tqdm(dataloader, disable=(not progress_bar)):
            tokens, starts, ends, attention_mask = self.tokenize(seqs)
            lls = self.score(tokens, starts, ends, attention_mask)
            for lhood in lls.flatten():
                out_file_obj.write(f"{str(lhood)}\n")
                out_file_obj.flush()

class VariantLikelihoodEvaluator(LikelihoodEvaluator):

    def evaluate(self, dataset, output_file, progress_bar=True):
        dataloader = DataLoader(dataset, batch_size=self.batch_size, shuffle=False, num_workers=self.num_workers)
        allele1_likelihoods = []
        allele2_likelihoods = []

        with open(output_file, "a") as f:
            for allele1, allele2 in tqdm(dataloader, disable=(not progress_bar)):
                tokens_allele1, starts_allele1, ends_allele1, attention_mask_allele1, offsets_allele1 = self.tokenize(allele1)
                tokens_allele2, starts_allele2, ends_allele2, attention_mask_allele2, offsets_allele2 = self.tokenize(allele2)
                lls_allele1 = self.score(tokens_allele1, starts_allele1, ends_allele1, attention_mask_allele1, offsets_allele1)
                lls_allele2 = self.score(tokens_allele2, starts_allele2, ends_allele2, attention_mask_allele2, offsets_allele2)
                for lhood_allele1, lhood_allele2 in zip(lls_allele1.flatten(), lls_allele2.flatten()):
                    allele1_likelihoods.append(lhood_allele1)
                    allele2_likelihoods.append(lhood_allele2)
                    data = {"allele1_likelihoods" : allele1_likelihoods, "allele2_likelihoods" : allele2_likelihoods}
                    df = pl.DataFrame(data, schema={"allele1_likelihoods": pl.Float64, "allele2_likelihoods": pl.Float64})
                    f.write(f"{lhood_allele1}\t{lhood_allele2}\n")
                    f.flush()
            return df
    
    def tokenize(self, seqs):
        seqs_str = onehot_to_chars(seqs)
        encoded = self.tokenizer.batch_encode_plus(seqs_str, return_tensors="pt", padding=True, return_offsets_mapping=True)
        tokens = encoded["input_ids"]
        offsets = encoded["offset_mapping"]
        try:
            attention_mask = encoded["attention_mask"]
        except:
            attention_mask = None
        if self.start_token is not None:
            starts = torch.where(tokens == self.start_token)[1] + 1 
        else:
            starts = torch.tensor([0]*tokens.shape[0])
        if self.end_token is not None:
            ends = torch.where(tokens == self.end_token)[1]
        else:
            ends = attention_mask.sum(dim=1) 
        return tokens, starts, ends, attention_mask, offsets
    

class MaskedZeroShotScore(metaclass=ABCMeta):
    @property
    @abstractmethod
    def mask_token(self):
        pass

    def score(self, tokens, starts, ends, attention_mask, offsets):
        tokens = tokens.to(device=self.device)
        attention_mask = attention_mask.to(device=self.device)
        lls = torch.zeros(tokens.shape[:2], device=self.device)
        for i in range(tokens.shape[1]):
            clip_mask = ((i >= starts) & (i < ends)).to(device=self.device)
            masked_tokens = tokens.clone()
            masked_tokens[:,i,...] = self.mask_token
            lls[:,i] = self.model_fwd(masked_tokens, attention_mask)[:,i] * clip_mask

        out = lls.sum(dim=1).numpy(force=True)

        return out
    
class MaskedProbingScore(metaclass=ABCMeta):
    @property
    @abstractmethod
    def mask_token(self):
        pass

    def score(self, tokens, starts, ends, attention_mask, offsets):
        tokens = tokens.to(device=self.device)
        if attention_mask is not None:
            attention_mask = attention_mask.to(device=self.device)
        if offsets is not None:
            offsets = offsets.to(device=self.device)
        indices = self._offsets_to_indices(offsets, tokens)
        indices = torch.from_numpy(indices).to(device=self.device)
        with torch.no_grad():
            try:
                torch_outs = self.model(
                    tokens,
                    attention_mask=attention_mask,
                    encoder_attention_mask=attention_mask,
                    output_hidden_states=True
                )
            except:
                torch_outs = self.model(tokens, output_hidden_states=True)
        if type(torch_outs.hidden_states) is tuple:
            hidden_states = torch_outs.hidden_states[-1]
        else:
            hidden_states = torch_outs.hidden_states
        probed_outs = self.probed_model(hidden_states, indices)
        return probed_outs
    

class CausalZeroShotScore(metaclass=ABCMeta):
    def score(self, tokens, starts, ends, attention_mask):
        tokens = tokens.to(device=self.device)
        lls = self.model_fwd(tokens, attention_mask)
        clip_mask = torch.tensor([[(i >= s) and (i < e) for i in range(lls.shape[1])] for s, e in zip(starts, ends)], 
                                 dtype=torch.float).to(device=self.device)

        out = (lls * clip_mask).sum(1).numpy(force=True)

        return out
    
class CausalProbingScore(metaclass=ABCMeta):
    
    def score(self, tokens, starts, ends, attention_mask, offsets):
        tokens = tokens.to(device=self.device)
        if attention_mask is not None:
            attention_mask = attention_mask.to(device=self.device)
        if offsets is not None:
            offsets = offsets.to(device=self.device)
        indices = self._offsets_to_indices(offsets, tokens)
        indices = torch.from_numpy(indices).to(device=self.device)
        with torch.no_grad():
            try:
                torch_outs = self.model(
                    tokens,
                    attention_mask=attention_mask,
                    encoder_attention_mask=attention_mask,
                    output_hidden_states=True
                )
            except:
                torch_outs = self.model(tokens, output_hidden_states=True)
        if type(torch_outs.hidden_states) is tuple or type(torch_outs.hidden_states) is list:
            last_hidden_state = torch_outs.hidden_states[-1]
        else:
            last_hidden_state = torch_outs.hidden_states

        probed_outs = self.probed_model(last_hidden_state, indices)
        return probed_outs


class DNABERT2Evaluator(LikelihoodEvaluator, MaskedZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"zhihan1996/{model_name}"
        with NoModule("triton"):
            tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
            config = BertConfig.from_pretrained(model_name, trust_remote_code=True)
            model = AutoModelForMaskedLM.from_pretrained(model_name, config=config, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return 1
    
    @property
    def end_token(self):
        return 2

class GenaLMEvaluator(LikelihoodEvaluator, MaskedZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"AIRI-Institute/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModel.from_pretrained(model_name, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return 1
    
    @property
    def end_token(self):
        return 2

class HDEvaluator(LikelihoodEvaluator, CausalZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"LongSafari/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True, padding_side="right")
        model = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return None
    
    @property
    def end_token(self):
        return 1

class MistralEvaluator(LikelihoodEvaluator, CausalZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"RaphaelMourad/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return 1
    
    @property
    def end_token(self):
        return 2

class NTEvaluator(LikelihoodEvaluator, MaskedZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"InstaDeepAI/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForMaskedLM.from_pretrained(model_name, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return 3
    
    @property
    def end_token(self):
        return None
    
class DNABERT2VariantEvaluator(VariantLikelihoodEvaluator):
    _idx_mode = "variable"
    def __init__(self, tokenizer, model, batch_size, num_workers, device):
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return 1
    
    @property
    def end_token(self):
        return 2
    
    @staticmethod
    def _offsets_to_indices(offsets, seqs):
        gather_idx = np.zeros((seqs.shape[0], seqs.shape[1]), dtype=np.int64)
        for i, offset in enumerate(offsets):
            for j, (start, end) in enumerate(offset):
                gather_idx[i,start:end] = j
        return gather_idx
    
class DNABERT2ZeroShotVariantEvaluator(DNABERT2VariantEvaluator, MaskedZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"zhihan1996/{model_name}"
        with NoModule("triton"):
            tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
            config = BertConfig.from_pretrained(model_name, trust_remote_code=True)
            model = AutoModelForMaskedLM.from_pretrained(model_name, config=config, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

class DNABERT2ProbingVariantEvaluator(DNABERT2VariantEvaluator, MaskedProbingScore):
    def __init__(self, probed_model, model_path, model_name, batch_size, num_workers, device):
        model_name = f"zhihan1996/{model_name}"
        with NoModule("triton"):
            tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
            config = BertConfig.from_pretrained(model_name, trust_remote_code=True)
            model = AutoModelForMaskedLM.from_pretrained(model_name, config=config, trust_remote_code=True)
        
        model_checkpoint = torch.load(model_path)
        probed_model.load_state_dict(model_checkpoint)
        self.probed_model = probed_model
        self.probed_model.to(device)

        super().__init__(tokenizer, model, batch_size, num_workers, device)
    
class GenaLMVariantEvaluator(VariantLikelihoodEvaluator):
    _idx_mode = "variable"
    def __init__(self, tokenizer, model, batch_size, num_workers, device):
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return 1
    
    @property
    def end_token(self):
        return 2
    
    @staticmethod
    def _offsets_to_indices(offsets, seqs):
        gather_idx = np.zeros((seqs.shape[0], seqs.shape[1]), dtype=np.int64)
        for i, offset in enumerate(offsets):
            for j, (start, end) in enumerate(offset):
                gather_idx[i,start:end] = j
        return gather_idx
    
class GenaLMZeroShotVariantEvaluator(GenaLMVariantEvaluator, MaskedZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"AIRI-Institute/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModel.from_pretrained(model_name, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

class GenaLMProbingVariantEvaluator(GenaLMVariantEvaluator, MaskedProbingScore):
    def __init__(self, probed_model, model_path, model_name, batch_size, num_workers, device):
        model_name = f"AIRI-Institute/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModel.from_pretrained(model_name, trust_remote_code=True)
        
        model_checkpoint = torch.load(model_path)
        probed_model.load_state_dict(model_checkpoint)
        self.probed_model = probed_model
        self.probed_model.to(device)

        super().__init__(tokenizer, model, batch_size, num_workers, device)

class HDVariantEvaluator(VariantLikelihoodEvaluator):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"LongSafari/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True, padding_side="right")
        model = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return None
    
    @property
    def end_token(self):
        return 1
    
class HDZeroShotVariantEvaluator(HDVariantEvaluator, CausalZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        super().__init__(model_name, batch_size, num_workers, device)

class HDProbingVariantEvaluator(HDVariantEvaluator, CausalProbingScore):
    def __init__(self, probed_model, model_path, model_name, batch_size, num_workers, device):
        model_checkpoint = torch.load(model_path)
        probed_model.load_state_dict(model_checkpoint)
        self.probed_model = probed_model
        self.probed_model.to(device)

        super().__init__(model_name, batch_size, num_workers, device)

    def tokenize(self, seqs):
        seqs_str = onehot_to_chars(seqs)
        encoded = self.tokenizer(seqs_str, return_tensors="pt", padding=True)
        tokens = encoded["input_ids"]
        try:
            attention_mask = encoded["attention_mask"]
            ends = attention_mask.sum(dim=1)
        except:
            attention_mask = None
            ends = None
        if self.start_token is not None:
            starts = torch.where(tokens == self.start_token)[1] + 1 
        else:
            starts = torch.tensor([0]*tokens.shape[0])
        if self.end_token is not None:
            ends = torch.where(tokens == self.end_token)[1]
            
        return tokens, starts, ends, attention_mask, None
    
    @staticmethod
    def _offsets_to_indices(offsets, seqs):
        slice_idx = [0, seqs.shape[1]]
        
        return np.array([slice_idx] * seqs.shape[0])
    
class MistralVariantEvaluator(VariantLikelihoodEvaluator):
    def __init__(self, tokenizer, model, batch_size, num_workers, device):
        model_name = f"RaphaelMourad/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return 1
    
    @property
    def end_token(self):
        return 2
    
    @staticmethod
    def _offsets_to_indices(offsets, seqs):
        gather_idx = np.zeros((seqs.shape[0], seqs.shape[1]), dtype=np.int64)
        for i, offset in enumerate(offsets):
            for j, (start, end) in enumerate(offset):
                gather_idx[i,start:end] = j
        return gather_idx
    
class MistralZeroShotVariantEvaluator(MistralVariantEvaluator, CausalZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        super().__init__(model_name, batch_size, num_workers, device)

class MistralProbingVariantEvaluator(MistralVariantEvaluator, CausalProbingScore):
    def __init__(self, probed_model, model_path, model_name, batch_size, num_workers, device):
        model_checkpoint = torch.load(model_path)
        probed_model.load_state_dict(model_checkpoint)
        self.probed_model = probed_model
        self.probed_model.to(device)

        super().__init__(model_name, batch_size, num_workers, device)

class NTVariantEvaluator(VariantLikelihoodEvaluator):
    def __init__(self, tokenizer, model, batch_size, num_workers, device):
        super().__init__(tokenizer, model, batch_size, num_workers, device)

    @property
    def start_token(self):
        return 3
    
    @property
    def end_token(self):
        return None
    
    @staticmethod
    def _offsets_to_indices(offsets, seqs):
        seq_len = seqs.shape[1]
        inds = np.zeros(seq_len, dtype=np.int64)
        # seq_len_contig = (seq_len // 6) * 6
        for i in range(seq_len // 6):
            inds[i*6:(i+1)*6] = i + 1
        inds[(i+1)*6:] = np.arange(i+2, i+(seq_len%6)+2)
        return np.array([inds] * seqs.shape[0])
    
class NTZeroShotVariantEvaluator(NTVariantEvaluator, MaskedZeroShotScore):
    def __init__(self, model_name, batch_size, num_workers, device):
        model_name = f"InstaDeepAI/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForMaskedLM.from_pretrained(model_name, trust_remote_code=True)
        super().__init__(tokenizer, model, batch_size, num_workers, device)

class NTProbingVariantEvaluator(NTVariantEvaluator, MaskedProbingScore):
    def __init__(self, probed_model, model_path, model_name, batch_size, num_workers, device):
        model_name = f"InstaDeepAI/{model_name}"
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForMaskedLM.from_pretrained(model_name, trust_remote_code=True)
        
        model_checkpoint = torch.load(model_path)
        probed_model.load_state_dict(model_checkpoint)
        self.probed_model = probed_model
        self.probed_model.to(device)

        super().__init__(tokenizer, model, batch_size, num_workers, device)

    def tokenize(self, seqs):
        seqs_str = onehot_to_chars(seqs)
        encoded = self.tokenizer(seqs_str, return_tensors="pt", padding=True)
        tokens = encoded["input_ids"]
        # print(tokens.shape) ####
        try:
            attention_mask = encoded["attention_mask"]
        except:
            attention_mask = None
        if self.start_token is not None:
            starts = torch.where(tokens == self.start_token)[1] + 1 
        else:
            starts = torch.tensor([0]*tokens.shape[0])
        if self.end_token is not None:
            ends = torch.where(tokens == self.end_token)[1]
        else:
            ends = attention_mask.sum(dim=1) 
        return tokens, starts, ends, attention_mask, None
