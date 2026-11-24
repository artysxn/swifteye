"""The fill model as plain arrays: load it and score one row without torch (the live bot runs it in every worker).

export_model.py writes <dir>/model.npz: the cut points of every input (the same 64 bins the training used), and five
boosted-tree models (seeds 0-4; depth 4): per tree and level the input each node looks at and its cut, and the value
of each leaf. A call = the mean over the five models of (base + the sum of the leaves the row lands in): the cents a
share the model expects a fill to keep after 5 minutes."""
import os

import numpy as np


class Model:
    def __init__(self, path):
        Z = np.load(path, allow_pickle=True)
        self.names = Z['names'].tolist()
        self.edges = [np.asarray(e, np.float64) for e in Z['edges']]
        self.feats, self.thrs, self.leaves, self.base = Z['feats'], Z['thrs'], Z['leaves'], Z['base']      # [seed][tree][level][node], ..., [seed][tree][leaf], [seed]
        self.ntrees = Z['ntrees']
        self.c0 = float(Z['c0'])
        self.meta = str(Z['meta'])
        self.nf = len(self.names)

    def bins(self, row):
        row = np.nan_to_num(np.asarray(row, np.float64))
        return np.array([np.searchsorted(self.edges[j], row[j], side='right') for j in range(self.nf)], np.int64)

    def call(self, row):
        xb = self.bins(row)
        tot = 0.0
        for s in range(len(self.base)):
            f = float(self.base[s])
            for t in range(int(self.ntrees[s])):
                node = 0
                for lv in range(self.feats.shape[2]):
                    node = node * 2 + int(xb[self.feats[s, t, lv, node]] > self.thrs[s, t, lv, node])
                f += float(self.leaves[s, t, node])
            tot += f
        return tot / len(self.base)


def default_path(data_dir):
    return os.path.join(data_dir, 'model.npz')
