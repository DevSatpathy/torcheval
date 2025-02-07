# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from torcheval.metrics.functional.aggregation import sum
from torcheval.metrics.functional.classification import (
    binary_accuracy,
    binary_auroc,
    binary_binned_precision_recall_curve,
    binary_f1_score,
    binary_normalized_entropy,
    binary_precision,
    binary_precision_recall_curve,
    binary_recall,
    multiclass_accuracy,
    multiclass_binned_precision_recall_curve,
    multiclass_f1_score,
    multiclass_precision,
    multiclass_precision_recall_curve,
    multiclass_recall,
    multilabel_accuracy,
    topk_multilabel_accuracy,
)
from torcheval.metrics.functional.ranking import (
    frequency_at_k,
    hit_rate,
    num_collisions,
    reciprocal_rank,
)
from torcheval.metrics.functional.regression import mean_squared_error, r2_score

__all__ = [
    "binary_auroc",
    "binary_accuracy",
    "binary_normalized_entropy",
    "binary_precision",
    "binary_precision_recall_curve",
    "binary_binned_precision_recall_curve",
    "binary_recall",
    "binary_f1_score",
    "frequency_at_k",
    "hit_rate",
    "mean",
    "mean_squared_error",
    "multiclass_accuracy",
    "multiclass_binned_precision_recall_curve",
    "multiclass_f1_score",
    "multiclass_precision",
    "multiclass_precision_recall_curve",
    "multiclass_recall",
    "multilabel_accuracy",
    "num_collisions",
    "topk_multilabel_accuracy",
    "r2_score",
    "reciprocal_rank",
    "sum",
]
