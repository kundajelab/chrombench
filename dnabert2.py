import time

import torch
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torch.profiler import profile, record_function, ProfilerActivity
from transformers import AutoTokenizer, AutoModelForMaskedLM, AutoModel, AutoModelForCausalLM, BertConfig

import components


class DNABERT2Evaluator(components.MaskedLogPerplexityEvaluator):
	def __init__(self, model_name, genome_fa, elements_tsv, chroms, batch_size, num_workers, seed, device):
		super().__init__(genome_fa, elements_tsv, chroms, batch_size, num_workers, seed, device)
		self.model_name = f"zhihan1996/{model_name}"
		self.tokenizer = AutoTokenizer.from_pretrained(self.model_name, trust_remote_code=True)
		config = BertConfig.from_pretrained(self.model_name, trust_remote_code=True)
		self.model = AutoModelForMaskedLM.from_pretrained(self.model_name, config=config, trust_remote_code=True)
		# self.model = AutoModelForMaskedLM.from_config(config)
		self.model.to(device)

	@property
	def mask_token(self):
		return self.tokenizer.mask_token_id


	def tokenize(self, seqs):
		seqs_str = components.onehot_to_chars(seqs)
		encoded = self.tokenizer.batch_encode_plus(seqs_str, return_tensors="pt", padding=True)
		tokens = encoded["input_ids"]
		starts, ends = torch.where(tokens == 1)[1] + 1, torch.where(tokens == 2)[1]
		return tokens, starts, ends, encoded["attention_mask"] 

	def model_fwd(self, tokens, attention_mask):
		with torch.no_grad():
			torch_outs = self.model(
				tokens,
				attention_mask=attention_mask,
				encoder_attention_mask = attention_mask
			)
			logits = torch_outs.logits.swapaxes(1, 2)
			# print(logits.shape) ####
			# print(tokens.shape) ####
			lls = -F.cross_entropy(logits, tokens, reduction="none")
		return lls


if __name__ == "__main__":
        model_name = "DNABERT-2-117M"
	#genome_fa = "/oak/stanford/groups/akundaje/refs/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta"
        genome_fa = "/mnt/data/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta"
        elements_tsv = "/oak/stanford/groups/akundaje/projects/dnalm_benchmark/regions/ccre_test_regions_500_jitter_50.bed"
        chroms = ["chr22"]
        batch_size = 1024
        num_workers = 4
        seed = 0
        device = "cuda"
	
        evaluator = DNABERT2Evaluator(model_name, genome_fa, elements_tsv, chroms, batch_size, num_workers, seed, device)
        metrics  = evaluator.evaluate(progress_bar=True)

        for k, v in metrics.items():
            print(f"{k}: {v}")
