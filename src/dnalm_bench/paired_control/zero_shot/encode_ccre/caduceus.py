import os

from ..evaluators import PairedControlDataset, CaduceusEvaluator

os.environ["TOKENIZERS_PARALLELISM"] = "false"


if __name__ == "__main__":
    model_name = "caduceus-ps_seqlen-131k_d_model-256_n_layer-16"
    #genome_fa = "/oak/stanford/groups/akundaje/refs/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta"
    # genome_fa = "/mnt/data/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta"
    genome_fa = "/home/atwang/dnalm_bench_data/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta"

    # elements_tsv = "/oak/stanford/groups/akundaje/projects/dnalm_benchmark/regions/ccre_test_regions_500_jitter_50.bed"
    elements_tsv = f"/home/atwang/dnalm_bench_data/ccre_test_regions_350_jitter_0.bed"

    out_dir = f"/home/atwang/dnalm_bench_data/encode_ccre/zero_shot/ccre_test_regions_350_jitter_0/{model_name}/v2"

    chroms = [
        "chr5",
        "chr10",
        "chr14",
        "chr18",
        "chr20",
        "chr22"
    ]

    batch_size = 2048
    num_workers = 4
    seed = 0
    device = "cuda"

    dataset = PairedControlDataset(genome_fa, elements_tsv, chroms, seed)
    evaluator = CaduceusEvaluator(model_name, dataset, batch_size, num_workers, device)
    metrics = evaluator.evaluate(out_dir, progress_bar=True)

    for k, v in metrics.items():
        print(f"{k}: {v}")