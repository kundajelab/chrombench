import os
import sys

from torch.utils.data import DataLoader

from ...training import EmbeddingsDataset, CNNEmbeddingsClassifier, train_classifier

work_dir = os.environ.get("DART_WORK_DIR", "")

if __name__ == "__main__":
    resume_checkpoint = int(sys.argv[1]) if len(sys.argv) > 1 else None

    model_name = "DNABERT-2-117M"
    embeddings_h5 = f"/scratch/groups/akundaje/chrombench/synapse/task_1_ccre/embeddings/{model_name}.h5"
    elements_tsv = os.path.join(work_dir, "task_1_ccre/processed_data/ENCFF420VPZ_processed.tsv")

    batch_size = 2048
    num_workers = 0
    prefetch_factor = 2
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

    input_channels = 768
    hidden_channels = 32
    kernel_size = 8

    lr = 2e-3

    num_epochs = 150

    # out_dir = f"/scratch/groups/akundaje/dnalm_benchmark/classifiers/ccre_test_regions_350_jitter_0/{model_name}/v1"
    out_dir = f"/scratch/groups/akundaje/chrombench/synapse/task_1_ccre/supervised_classifiers/{model_name}/v1"
    os.makedirs(out_dir, exist_ok=True)

    cache_dir = None

    train_dataset = EmbeddingsDataset(embeddings_h5, elements_tsv, chroms_train, cache_dir=cache_dir)
    val_dataset = EmbeddingsDataset(embeddings_h5, elements_tsv, chroms_val, cache_dir=cache_dir)
    model = CNNEmbeddingsClassifier(input_channels, hidden_channels, kernel_size)

    train_classifier(train_dataset, val_dataset, model, num_epochs, out_dir, batch_size, lr, num_workers, prefetch_factor, device, 
                     progress_bar=True, resume_from=resume_checkpoint)