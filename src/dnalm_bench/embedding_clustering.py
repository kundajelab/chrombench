from abc import ABCMeta, abstractmethod
import numpy as np
import h5py
import hashlib
from scipy.stats import wilcoxon
import os
import matplotlib.pyplot as plt
import joblib
from sklearn.cluster import *
os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

class EmbeddingCluster():
	def __init__(self, cluster_obj, embeddings, labels):
		self.cluster_obj = cluster_obj
		self.embeddings = embeddings
		self.true_labels = labels
		self.cluster_obj.fit(self.embeddings)

	def get_cluster_labels(self):
		return self.cluster_obj.labels_

	def get_clustering_score(self, metric):
		cluster_labels = self.get_cluster_labels()
		return metric(self.true_labels, cluster_labels)
	
	def plot_embeddings(self, reduction_obj, out_path, categories):
		embeddings_2d = reduction_obj.fit_transform(self.embeddings)
		first_dim, second_dim = embeddings_2d[:,0], embeddings_2d[:,1]
		plot_labels=[categories[x] for x in self.true_labels]
		plt.figure(dpi=300)
		scatter = plt.scatter(first_dim, second_dim, c=self.true_labels, s=0.5)
		plt.xticks([])
		plt.yticks([])
		plt.title("Model Embeddings Colored by Cell Type")
		print(scatter.legend_elements()[0])
		plt.legend(handles=scatter.legend_elements()[0], labels=categories)
		plt.savefig(out_path, format="png")
		plt.show()

	def save_model(self, out_path):
		joblib.dump(self.cluster_obj, out_path)


def load_embeddings_and_labels(embedding_file, label_file):
	'''
	Assumes embedding_h5 embeddings for all peaks
	'''
	arrays = []
    embedding_h5 = h5py.File(embedding_file, "r")
    cat_list = list(pd.read_csv(label_file, sep="\t")["label"].values)
    cat_set = list(set(cat_list))
    labels = [cat_set.index(x) for x in cat_list]
    for key in list(embedding_h5['seq'].keys()):
        if "idx" in key:
            continue
        split = key.split("_")
        ind_start, ind_end = int(split[-2]), int(split[-1])
        h5_array = file['seq'][key][:]
        if "idx_var" in file['seq'].keys():
            idx_vars = file['seq']['idx_var'][ind_start:ind_end]
            mins, maxes = idx_vars.min(1), idx_vars.max(1) + 1
            indices = [np.arange(mi, ma) for mi, ma in zip(mins, maxes)]
            curr_means = np.array([np.mean(h5_array[i, indices[i], :], axis=0) for i in range(h5_array.shape[0])])
        elif "idx_fix" in file['seq'].keys():
            idx_fix = file['seq']['idx_fix'][:]
            indices = np.arange(idx_fix.min(), idx_fix.max() + 1)
            curr_means = np.mean(h5_array[:, indices, :], axis=1)
			# Calculate mean over specified slices for each row
		arrays.append(np.vstack(curr_means))
	assert len(arrays) == len(labels)
	return np.vstack(arrays), labels, cat_list