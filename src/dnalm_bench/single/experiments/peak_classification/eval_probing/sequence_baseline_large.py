import os
import sys

import torch

# from ....training import AssayEmbeddingsDataset, InterleavedIterableDataset, CNNEmbeddingsPredictor, train_predictor
from ....finetune import PeaksEndToEndDataset, eval_finetuned_peak_classifier, LargeCNNClassifier


if __name__ == "__main__":
    eval_mode = sys.argv[1] if len(sys.argv) > 1 else "test"

    model_name = "sequence_baseline_large"
    # genome_fa = "/oak/stanford/groups/akundaje/refs/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta"
    # genome_fa = "/mnt/data/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta"
    genome_fa = "/home/atwang/dnalm_bench_data/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta"
    elements_tsv = "/home/atwang/dnalm_bench_data/peaks_by_cell_label_unique_dataloader_format.tsv"

    batch_size = 2048
    num_workers = 4
    prefetch_factor = 2
    # num_workers = 0 ####
    seed = 0
    device = "cuda"

    chroms_train = [
        "chr1",
        "chr2",
        "chr3",
        "chr4",
        "chr7",
        "chr8",
        "chr9",
        "chr11",
        "chr12",
        "chr13",
        "chr15",
        "chr16",
        "chr17",
        "chr19",
        "chrX",
        "chrY"
    ]
    
    chroms_val = [
        "chr6",
        "chr21"
    ]

    chroms_test = [
        "chr5",
        "chr10",
        "chr14",
        "chr18",
        "chr20",
        "chr22"
    ]

    modes = {"train": chroms_train, "val": chroms_val, "test": chroms_test}

    # emb_channels = 256

    # lora_rank = 8
    # lora_alpha = 2 * lora_rank
    # lora_dropout = 0.05

    n_filters = 512
    n_residual_convs = 7
    output_channels = 2
    seq_len = 480

    accumulate = 1
    
    lr = 1e-4
    wd = 0
    num_epochs = 200

    # cache_dir = os.environ["L_SCRATCH_JOB"]
    cache_dir = "/mnt/disks/ssd-0/dnalm_bench_cache"

    out_dir = f"/home/atwang/dnalm_bench_data/predictor_eval/peak_classification/{model_name}"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"eval_{eval_mode}.json")

    model_dir = f"/home/atwang/dnalm_bench_data/predictors/peak_classification/{model_name}/v3"    
    checkpoint_num = 189
    checkpoint_path = os.path.join(model_dir, f"checkpoint_{checkpoint_num}.pt")
    
    classes = {
        "GM12878": 0,
        "H1ESC": 1,
        "HEPG2": 2,
        "IMR90": 3,
        "K562": 4
    } 

    test_dataset = PeaksEndToEndDataset(genome_fa, elements_tsv, modes[eval_mode], classes, cache_dir=cache_dir)

    model = LargeCNNClassifier(4, n_filters, n_residual_convs, len(classes), seq_len)
    checkpoint_resume = torch.load(checkpoint_path)
    model.load_state_dict(checkpoint_resume, strict=False)

    metrics = eval_finetuned_peak_classifier(test_dataset, model, out_path, batch_size,
                                    num_workers, prefetch_factor, device, progress_bar=True)
    
    for k, v in metrics.items():
        print(f"{k}: {v}")