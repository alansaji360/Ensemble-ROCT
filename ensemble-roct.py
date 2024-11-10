# -*- coding: utf-8 -*-
"""Untitled2.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1YsKQA1MKjoHqHBg39adVWbfjKoQAQbb-
"""

from google.colab import drive
drive.mount('/content/drive')

!pip install xgboost
!pip install pulp
!pip install -U scikit-learn
!pip install imbalanced-learn
!pip install nvidia-ml-py3

import numpy as np
from pulp import *
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier
import xgboost as xgb
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
import pandas as pd
from sklearn.metrics import (classification_report, roc_auc_score,
                           precision_recall_curve, auc, accuracy_score,
                           precision_score, recall_score, f1_score)
from typing import List, Tuple, Dict
from sklearn.model_selection import train_test_split
import warnings
import torch
import time
from imblearn.over_sampling import SMOTE
from contextlib import contextmanager
from tqdm.notebook import tqdm
from concurrent.futures import ThreadPoolExecutor
import queue

import networkx as nx
from typing import Tuple, Set
import matplotlib.pyplot as plt
from tqdm import tqdm

import threading

warnings.filterwarnings('ignore')

# Check GPU availability
USE_GPU = torch.cuda.is_available()
if USE_GPU:
    print("GPU is available:", torch.cuda.get_device_name(0))
    !nvidia-smi
else:
    print("GPU is not available, using CPU")

def get_device():
    """Get the available device (GPU or CPU)"""
    if torch.cuda.is_available():
        device = torch.device('cuda')
        print(f"Using GPU: {torch.cuda.get_device_name(0)}")
    else:
        device = torch.device('cpu')
        print("Using CPU")
    return device

def smart_sampling(X: np.ndarray,
                  y: np.ndarray,
                  max_samples: int = 20000,  # Significantly increased
                  sampling_strategy: str = 'balanced',
                  random_state: int = 42) -> Tuple[np.ndarray, np.ndarray]:
    """
    Smart sampling strategy with larger sample size and better class representation
    """
    print("\nSmart Sampling Debug:")
    print(f"Max samples requested: {max_samples}")

    # Set random seed
    np.random.seed(random_state)

    # Convert to numpy arrays if needed
    X = np.array(X)
    y = np.array(y)

    # Get indices for each class
    pos_mask = y == 1
    neg_mask = y == 0

    n_pos = np.sum(pos_mask)
    n_neg = np.sum(neg_mask)

    print(f"Original dataset size: {len(y)}")
    print(f"Original distribution - Positive: {n_pos}, Negative: {n_neg}")

    if n_pos == 0 or n_neg == 0:
        raise ValueError("Both classes must have at least one sample!")

    if sampling_strategy == 'balanced':
        # Use all positive samples
        n_pos_samples = n_pos

        # Calculate how many negative samples we can include
        remaining_space = max_samples - n_pos
        n_neg_samples = min(remaining_space, n_neg)

        print(f"\nSelecting all {n_pos_samples} positive and {n_neg_samples} negative samples")

        # Sample positive class
        pos_indices = np.where(pos_mask)[0]
        sampled_pos_idx = pos_indices  # Take all positive samples

        # Sample negative class
        neg_indices = np.where(neg_mask)[0]
        sampled_neg_idx = np.random.choice(neg_indices, size=n_neg_samples, replace=False)

        # Combine indices
        selected_idx = np.concatenate([sampled_pos_idx, sampled_neg_idx])
        np.random.shuffle(selected_idx)

        X_sampled = X[selected_idx]
        y_sampled = y[selected_idx]

    else:
        raise ValueError(f"Unknown sampling strategy: {sampling_strategy}")

    # Print final distribution
    final_pos = np.sum(y_sampled == 1)
    final_neg = np.sum(y_sampled == 0)

    print("\nFinal sampled distribution:")
    print(f"Total samples: {len(y_sampled)}")
    print(f"Positive samples: {final_pos} ({final_pos/len(y_sampled)*100:.2f}%)")
    print(f"Negative samples: {final_neg} ({final_neg/len(y_sampled)*100:.2f}%)")

    return X_sampled, y_sampled

def load_and_preprocess_data(data_path: str, target_col: str = 'Class', test_size: float = 0.2):
    """Load and preprocess data"""
    print("Loading data...")
    df = pd.read_csv(data_path)

    # Separate features and target
    X = df.drop(target_col, axis=1).values
    y = df[target_col].values

    # Print initial class distribution
    print("\nInitial class distribution:")
    print(pd.Series(y).value_counts(normalize=True))

    # Split data
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=42, stratify=y
    )

    return X_train, X_test, y_train, y_test

class ROCTTree(BaseEstimator, ClassifierMixin):
    def __init__(self,
                 max_depth: int = 4,
                 epsilon: float = 0.05,
                 lambda_param: float = 0.01,
                 time_limit: int = 300,
                 max_samples: int = 500,
                 sampling_strategy: str = 'balanced',
                 use_gpu: bool = True,
                 n_pieces: int = 10):
        self.max_depth = max_depth
        self.epsilon = epsilon
        self.lambda_param = lambda_param
        self.time_limit = time_limit
        self.max_samples = max_samples
        self.sampling_strategy = sampling_strategy
        self.use_gpu = use_gpu and torch.cuda.is_available()
        self.device = get_device() if self.use_gpu else torch.device('cpu')
        self.scaler = StandardScaler()
        self.n_pieces = n_pieces

        self.n_nodes = 2**(max_depth + 1) - 1
        self.n_leaves = 2**max_depth
        self.n_splits = self.n_nodes - self.n_leaves

    

    def _create_piecewise_approx(self):
        """Create piecewise linear approximation of logistic loss"""
        # Break [0,1] into n_pieces intervals
        points = np.linspace(0, 1, self.n_pieces + 1)
        slopes = []
        intercepts = []

        # Calculate slopes and intercepts for each piece
        for i in range(self.n_pieces):
            x1, x2 = points[i], points[i + 1]
            y1 = -np.log(x1 + 1e-10)  # Add small epsilon to avoid log(0)
            y2 = -np.log(x2 + 1e-10)
            slope = (y2 - y1) / (x2 - x1)
            intercept = y1 - slope * x1
            slopes.append(slope)
            intercepts.append(intercept)

        return slopes, intercepts

    def _build_optimal_tree(self, X: np.ndarray, y: np.ndarray):
        """Simplified optimal tree construction with linearized constraints"""
        n_samples, n_features = X.shape
        model = LpProblem("ROCT", LpMinimize)

        print("\nStarting optimization with:")
        print(f"Samples: {n_samples}, Features: {n_features}")
        print(f"Tree depth: {self.max_depth}, Number of splits: {self.n_splits}")

        print("Creating decision variables...")
        # Feature selection and threshold variables
        a = LpVariable.dicts("feature_select",
                          ((i, j) for i in range(self.n_splits)
                            for j in range(n_features)),
                          cat='Binary')

        b = LpVariable.dicts("threshold",
                          (i for i in range(self.n_splits)),
                          lowBound=-1, upBound=1)

        # Binary reachability variables
        r = LpVariable.dicts("reachable",
                          ((i, t) for i in range(n_samples)
                            for t in range(self.n_leaves)),
                          cat='Binary')

        # Binary leaf predictions
        c = LpVariable.dicts("leaf_pred",
                          (i for i in range(self.n_leaves)),
                          cat='Binary')

        # Error variables
        e = LpVariable.dicts("error",
                          (i for i in range(n_samples)),
                          cat='Binary')

        print("Setting objective function...")
        # Simple misclassification objective
        model += lpSum(e[i] for i in range(n_samples))

        print("Adding constraints...")
        M = 2  # Small M value

        # Each split must use exactly one feature
        for t in range(self.n_splits):
            model += lpSum(a[t,j] for j in range(n_features)) == 1

        # Each sample must reach exactly one leaf
        for i in range(n_samples):
            model += lpSum(r[i,t] for t in range(self.n_leaves)) == 1

        # Split routing constraints
        for i in range(n_samples):
            for t in range(self.n_leaves):
                path = self._get_path_to_leaf(t)
                for node, direction in path:
                    split_sum = lpSum(X[i,j] * a[node,j] for j in range(n_features))

                    if direction == 'left':
                        model += split_sum <= b[node] + M * (1 - r[i,t])
                    else:
                        model += split_sum >= b[node] - M * (1 - r[i,t])

        # Error definition constraints - linearized version
        for i in range(n_samples):
            for t in range(self.n_leaves):
                if y[i] == 1:
                    # For positive samples
                    model += e[i] >= 1 - c[t] - M * (1 - r[i,t])
                else:
                    # For negative samples
                    model += e[i] >= c[t] - M * (1 - r[i,t])

        # Force some leaves to predict each class
        # model += lpSum(c[t] for t in range(self.n_leaves)) >= 1  # At least one positive
        # model += lpSum(1 - c[t] for t in range(self.n_leaves)) >= 1  # At least one negative

        model += c[0] <= 0.3  # Force first leaf to predict low
        model += c[self.n_leaves-1] >= 0.7  # Force last leaf to predict high

        # Force ordering of leaf predictions to prevent symmetric solutions
        for t in range(self.n_leaves-1):
            model += c[t] <= c[t+1]

        print(f"Starting optimization (time limit: {self.time_limit}s)...")
        solver = PULP_CBC_CMD(
            timeLimit=self.time_limit,
            msg=True,
            threads=8 if self.use_gpu else 4,
            options=[
                'maxNodes=10000',
                'allowableGap=0.1',
                'strongBranching=1'
            ]
        )

        status = model.solve(solver)
        print(f"Optimization Status: {LpStatus[status]}")

        return model, a, b, c, r

    

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Simplified prediction function"""
        if not hasattr(self, 'scaler'):
            return np.zeros(len(X))

        X = self.scaler.transform(X)
        predictions = np.zeros(len(X))

        for i, x in enumerate(X):
            leaf_pred = 0
            current_node = 0

            while current_node < self.n_splits:
                split = self.tree_structure['splits'][current_node]
                if split['feature'] is None:
                    break

                if x[split['feature']] <= split['threshold']:
                    current_node = 2 * current_node + 1
                else:
                    current_node = 2 * current_node + 2

            # Find corresponding leaf
            if current_node >= self.n_splits:
                leaf_idx = current_node - self.n_splits
                if leaf_idx < self.n_leaves:
                    leaf_pred = self.tree_structure['leaves'][leaf_idx]

            predictions[i] = int(leaf_pred > 0.5)

        return predictions

    def fit(self, X: np.ndarray, y: np.ndarray):
        """Fit with improved debugging"""
        print("Training ROCT model...")

        # Validate input
        if not isinstance(X, np.ndarray) or not isinstance(y, np.ndarray):
            X = np.array(X)
            y = np.array(y)

        # Print data distribution
        print("\nTraining data distribution:")
        unique, counts = np.unique(y, return_counts=True)
        for label, count in zip(unique, counts):
            print(f"Class {label}: {count} samples ({count/len(y)*100:.2f}%)")

        # Scale features
        X = self.scaler.fit_transform(X)

        # Print feature statistics
        print("\nFeature statistics:")
        print(f"Number of features: {X.shape[1]}")
        print(f"Feature range: [{X.min():.3f}, {X.max():.3f}]")
        print(f"Feature mean: {X.mean():.3f}")
        print(f"Feature std: {X.std():.3f}")

        # Build the optimal tree
        try:
            model, a, b, c, r = self._build_optimal_tree(X, y)

            # Extract and validate solution
            print("\nExtracting optimization solution...")

            self.tree_structure = {
                'splits': {i: {
                    'feature': next((j for j in range(X.shape[1])
                                  if value(a[i,j]) > 0.5), None),
                    'threshold': value(b[i])
                } for i in range(self.n_splits)},
                'leaves': {i: value(c[i]) for i in range(self.n_leaves)},
                'reachable': {i: {t: value(r[i,t])
                                for t in range(self.n_leaves)}
                            for i in range(len(X))}
            }

            # Print detailed tree structure
            print("\nDetailed Tree Structure:")
            for node in range(self.n_splits):
                split = self.tree_structure['splits'][node]
                if split['feature'] is not None:
                    print(f"Node {node}: Split on feature {split['feature']} at threshold {split['threshold']:.3f}")

            print("\nLeaf Predictions:")
            for leaf in range(self.n_leaves):
                print(f"Leaf {leaf}: {self.tree_structure['leaves'][leaf]:.3f}")

            # Verify tree structure
            active_splits = sum(1 for split in self.tree_structure['splits'].values()
                              if split['feature'] is not None)
            print(f"\nActive splits: {active_splits}/{self.n_splits}")

            if active_splits == 0:
                print("WARNING: No active splits found!")
                print("Optimization may have failed to find meaningful splits.")

            leaf_values = list(self.tree_structure['leaves'].values())
            print(f"Leaf predictions range: [{min(leaf_values):.3f}, {max(leaf_values):.3f}]")

            if max(leaf_values) - min(leaf_values) < 0.1:
                print("WARNING: Very small range in leaf predictions!")
                print("Tree may not be making meaningful distinctions between classes.")

        except Exception as e:
            print(f"Error during optimization: {str(e)}")
            raise

        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Predict probabilities"""
        preds = self.predict(X)
        return np.vstack([1-preds, preds]).T

    def _get_path_to_leaf(self, leaf_idx: int) -> List[Tuple[int, str]]:
        """Get path from root to leaf"""
        path = []
        current = leaf_idx + self.n_splits
        while current > 0:
            parent = (current - 1) // 2
            direction = 'left' if current % 2 == 1 else 'right'
            path.append((parent, direction))
            current = parent
        return path[::-1]

class ROCTRandomForest(BaseEstimator, ClassifierMixin):
    def __init__(self,
                 n_estimators: int = 5,
                 max_depth: int = 3,
                 epsilon: float = 0.1,
                 max_samples: int = 500,
                 sampling_strategy: str = 'balanced',
                 time_limit: int = 300,
                 use_gpu: bool = True):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.epsilon = epsilon
        self.max_samples = max_samples
        self.sampling_strategy = sampling_strategy
        self.time_limit = time_limit
        self.use_gpu = use_gpu and torch.cuda.is_available()
        self.device = get_device() if self.use_gpu else torch.device('cpu')
        self.trees = []
        self.scaler = StandardScaler()

    def _train_tree_batch(self, X, y, start_idx, n_trees, gpu_id=None):
        """Train a batch of trees with time management"""
        trees = []
        time_per_tree = self.time_limit // n_trees

        for i in range(n_trees):
            try:
                # Clear GPU memory
                if self.use_gpu:
                    torch.cuda.empty_cache()

                # Sample data for this tree
                X_sampled, y_sampled = smart_sampling(
                    X.cpu().numpy() if torch.is_tensor(X) else X,
                    y.cpu().numpy() if torch.is_tensor(y) else y,
                    max_samples=self.max_samples,
                    sampling_strategy=self.sampling_strategy,
                    random_state=start_idx + i
                )

                # Create and train tree with allocated time
                tree = ROCTTree(
                    max_depth=self.max_depth,
                    epsilon=self.epsilon,
                    time_limit=time_per_tree,
                    max_samples=self.max_samples,
                    sampling_strategy=self.sampling_strategy,
                    use_gpu=self.use_gpu
                )

                if self.use_gpu and gpu_id is not None:
                    with torch.cuda.device(gpu_id):
                        X_sampled = torch.tensor(X_sampled, device=f'cuda:{gpu_id}')
                        y_sampled = torch.tensor(y_sampled, device=f'cuda:{gpu_id}')
                        tree.fit(X_sampled, y_sampled)
                else:
                    tree.fit(X_sampled, y_sampled)

                trees.append(tree)
                print(f"Trained tree {start_idx + i + 1}/{self.n_estimators} (Time limit: {time_per_tree}s)")

            except Exception as e:
                print(f"Warning: Failed to train tree {start_idx + i + 1}: {str(e)}")
                continue

            finally:
                # Clear GPU memory after each tree
                if self.use_gpu:
                    torch.cuda.empty_cache()

        return trees

    def fit(self, X: np.ndarray, y: np.ndarray):
        """Fit the random forest of ROCT trees"""
        print("Training ROCT Random Forest...")

        # First validate input
        if not isinstance(X, np.ndarray) or not isinstance(y, np.ndarray):
            X = np.array(X)
            y = np.array(y)

        # Print data distribution
        print("\nOriginal data distribution:")
        unique, counts = np.unique(y, return_counts=True)
        for label, count in zip(unique, counts):
            print(f"Class {label}: {count} samples ({count/len(y)*100:.2f}%)")

        # Scale features
        X = self.scaler.fit_transform(X)

        # Train trees in batches
        n_gpus = torch.cuda.device_count() if self.use_gpu else 0

        if n_gpus > 1:
            # Parallel training on multiple GPUs
            trees_per_gpu = self.n_estimators // n_gpus
            extra_trees = self.n_estimators % n_gpus

            self.trees = []
            for i in range(n_gpus):
                n_trees = trees_per_gpu + (1 if i < extra_trees else 0)
                if n_trees > 0:
                    trees = self._train_tree_batch(X, y, len(self.trees), n_trees, gpu_id=i)
                    self.trees.extend(trees)
        else:
            # Sequential training
            self.trees = self._train_tree_batch(X, y, 0, self.n_estimators)

        print(f"\nSuccessfully trained {len(self.trees)} trees")
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Improved ensemble prediction"""
        X = self.scaler.transform(X)
        predictions = []

        # Get predictions from all trees
        for tree in self.trees:
            pred = tree.predict_proba(X)
            predictions.append(pred)

        # Weighted average based on tree performance
        avg_predictions = np.mean(predictions, axis=0)

        # Ensure valid probabilities with slight smoothing
        avg_predictions = np.clip(avg_predictions, 0.001, 0.999)

        return avg_predictions

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Make predictions with robustness guarantees"""
        proba = self.predict_proba(X)
        return (proba[:,1] >= 0.5).astype(int)

class ROCTGradientBoosting(BaseEstimator, ClassifierMixin):
    def __init__(self,
                 n_estimators: int = 5,
                 max_depth: int = 3,
                 epsilon: float = 0.1,
                 learning_rate: float = 0.1,
                 max_samples: int = 500,
                 sampling_strategy: str = 'balanced',
                 time_limit: int = 300):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.epsilon = epsilon
        self.learning_rate = learning_rate
        self.max_samples = max_samples
        self.sampling_strategy = sampling_strategy
        self.time_limit = time_limit
        self.trees = []
        self.scaler = StandardScaler()
        self.base_score = None

    def _compute_base_score(self, y):
        """Improved base score computation"""
        pos_count = np.sum(y == 1) + 1  # Laplace smoothing
        neg_count = np.sum(y == 0) + 1
        # Scaled log odds
        return 0.5 * np.log((pos_count / neg_count))

    def _compute_gradients(self, y_true, F):
        """Compute gradients for logistic loss with weights"""
        p = 1 / (1 + np.exp(-F))
        # Weight gradients based on class
        grad_weights = np.where(y_true == 1,
                              len(y_true)/(2*np.sum(y_true == 1)),
                              len(y_true)/(2*np.sum(y_true == 0)))
        return (y_true - p) * grad_weights

    def fit(self, X: np.ndarray, y: np.ndarray):
        """Improved gradient boosting training"""
        print(f"Training ROCT Gradient Boosting with {self.n_estimators} trees...")
        X = self.scaler.fit_transform(X)

        # Initialize predictions
        self.base_score = self._compute_base_score(y)
        F = np.full(len(X), self.base_score)

        # Time management
        time_per_tree = self.time_limit // self.n_estimators

        # Initial sampling
        X_sampled, y_sampled = smart_sampling(
            X, y,
            max_samples=self.max_samples,
            sampling_strategy=self.sampling_strategy
        )
        F_sampled = np.full(len(X_sampled), self.base_score)

        for i in range(self.n_estimators):
            print(f"\nTraining tree {i+1}/{self.n_estimators}")

            # Compute gradients
            gradients = self._compute_gradients(y_sampled, F_sampled)

            # Scale learning rate by iteration
            current_lr = self.learning_rate * (0.8 + 0.4 * (1 - i/self.n_estimators))

            try:
                # Train tree on gradients
                tree = ROCTTree(
                    max_depth=self.max_depth,
                    epsilon=self.epsilon,
                    time_limit=time_per_tree,
                    max_samples=self.max_samples,
                    sampling_strategy='balanced',  # Always balance for gradients
                    lambda_param=0.01 * (1 + i/(2*self.n_estimators))
                )

                tree.fit(X_sampled, gradients)
                self.trees.append(tree)

                # Update predictions
                tree_preds = tree.predict(X)
                F += current_lr * tree_preds

                # Check current performance
                current_preds = (1 / (1 + np.exp(-F)) >= 0.5).astype(int)
                current_f1 = f1_score(y, current_preds)
                print(f"Current F1 score: {current_f1:.4f}")

                # Early stopping if perfect separation
                if current_f1 > 0.99:
                    print("Early stopping: achieved high F1 score")
                    break

                # Resample and update predictions
                if i < self.n_estimators - 1:
                    X_sampled, y_sampled = smart_sampling(
                        X, y,
                        max_samples=self.max_samples,
                        sampling_strategy=self.sampling_strategy,
                        random_state=i
                    )
                    F_sampled = np.full(len(X_sampled), self.base_score)
                    for tree in self.trees:
                        F_sampled += current_lr * tree.predict(X_sampled)

            except Exception as e:
                print(f"Warning: Tree {i+1} failed: {str(e)}")
                continue

        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Improved probability predictions"""
        X = self.scaler.transform(X)

        # Start with base score
        F = np.full(len(X), self.base_score)

        # Sum tree predictions
        for tree in self.trees:
            F += self.learning_rate * tree.predict(X)

        # Convert to probabilities with smoothing
        proba = 1 / (1 + np.exp(-F))
        proba = np.clip(proba, 0.001, 0.999)

        return np.vstack([1-proba, proba]).T

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Make predictions with threshold adjustment"""
        proba = self.predict_proba(X)
        # Adjusted threshold for better balance
        return (proba[:,1] >= 0.4).astype(int)

def run_comparison(data_path: str):
    """Run faster comparison with reduced parameters and proper sampling"""
    # Load data
    print("Loading data...")
    df = pd.read_csv(data_path)

    # Print initial distribution
    print("\nInitial class distribution:")
    print(df['Class'].value_counts(normalize=True))

    # Separate features and target
    X = df.drop('Class', axis=1).values
    y = df['Class'].values

    # First do smart sampling on entire dataset before train/test split
    print("\nApplying smart sampling to full dataset...")
    X_sampled, y_sampled = smart_sampling(
        X, y,
        max_samples=1000,  # Increased for better representation
        sampling_strategy='balanced'
    )

    # Now split the sampled data
    print("\nSplitting sampled data into train/test sets...")
    X_train, X_test, y_train, y_test = train_test_split(
        X_sampled, y_sampled,
        test_size=0.2,
        random_state=42,
        stratify=y_sampled  # Ensure balanced split
    )

    # Print train/test distributions
    print("\nTraining set distribution:")
    unique, counts = np.unique(y_train, return_counts=True)
    for label, count in zip(unique, counts):
        print(f"Class {label}: {count} samples ({count/len(y_train)*100:.2f}%)")

    print("\nTest set distribution:")
    unique, counts = np.unique(y_test, return_counts=True)
    for label, count in zip(unique, counts):
        print(f"Class {label}: {count} samples ({count/len(y_test)*100:.2f}%)")

    # Initialize models
    models = {
        'Standard Random Forest': RandomForestClassifier(
            n_estimators=50,  # Reduced from 50
            max_depth=3,
            random_state=42
        ),
        'Standard Gradient Boosting': GradientBoostingClassifier(
            n_estimators=50,  # Reduced from 50
            max_depth=3,
            random_state=42
        ),
        'ROCT Tree': ROCTTree(
            max_depth=3,
            epsilon=0.1,
            time_limit=300,  # Reduced from 300
            max_samples=500,  # Reduced from 1000
            sampling_strategy='balanced'
        ),
        'ROCT Random Forest': ROCTRandomForest(
            n_estimators=5,  # Reduced from 50
            max_depth=3,
            epsilon=0.1,
            max_samples=500,  # Reduced from 1000
            sampling_strategy='balanced',
            time_limit=300  # Added time_limit parameter
        ),
        'ROCT Gradient Boosting': ROCTGradientBoosting(
            n_estimators=5,  # Reduced from 50
            max_depth=3,
            epsilon=0.1,
            max_samples=500,  # Reduced from 1000
            sampling_strategy='balanced',
            time_limit=300  # Added time_limit parameter
        )
    }

    results = {}

    # Train and evaluate each model
    for name, model in models.items():
        print(f"\nTraining and evaluating {name}...")

        # Training time
        t_start = time.time()
        model.fit(X_train, y_train)
        train_time = time.time() - t_start
        print(f"{name} training time: {train_time:.2f} seconds")

        # Evaluation
        clean_metrics, adv_metrics = evaluate_model(model, X_test, y_test)

        results[name] = {
            'training_time': train_time,
            'clean': clean_metrics,
            'adversarial': adv_metrics
        }

    # Print results summary
    print("\nResults Summary:")
    print("=" * 80)

    for name, result in results.items():
        print(f"\n{name}:")
        print("-" * 40)
        print(f"Training Time: {result['training_time']:.2f} seconds")

        print("\nClean Data Performance:")
        print(f"Accuracy:    {result['clean']['accuracy']:.4f}")
        print(f"ROC AUC:     {result['clean']['roc_auc']:.4f}")
        print(f"F1 Score:    {result['clean']['f1']:.4f}")
        print(f"Precision:   {result['clean']['precision']:.4f}")
        print(f"Recall:      {result['clean']['recall']:.4f}")
        print(f"Inference Time: {result['clean']['inference_time']:.2f} seconds")

        print("\nAdversarial Data Performance:")
        print(f"Accuracy:    {result['adversarial']['accuracy']:.4f}")
        print(f"ROC AUC:     {result['adversarial']['roc_auc']:.4f}")
        print(f"F1 Score:    {result['adversarial']['f1']:.4f}")
        print(f"Precision:   {result['adversarial']['precision']:.4f}")
        print(f"Recall:      {result['adversarial']['recall']:.4f}")
        print(f"Inference Time: {result['adversarial']['inference_time']:.2f} seconds")

    # Print robustness comparison
    print("\nRobustness Analysis (Clean - Adversarial Accuracy):")
    print("=" * 80)
    for name, result in results.items():
        diff = result['clean']['accuracy'] - result['adversarial']['accuracy']
        print(f"{name}: {diff:.4f}")

    return results

def evaluate_model(model, X_test, y_test, epsilon=0.1):
    """Evaluate a single model on clean and adversarial data with better error handling"""
    try:
        # Clean data evaluation
        start_time = time.time()
        y_pred = model.predict(X_test)
        y_pred_proba = model.predict_proba(X_test)[:,1]
        inference_time = time.time() - start_time

        clean_metrics = {
            'accuracy': accuracy_score(y_test, y_pred),
            'roc_auc': roc_auc_score(y_test, y_pred_proba),
            'f1': f1_score(y_test, y_pred),
            'precision': precision_score(y_test, y_pred),
            'recall': recall_score(y_test, y_pred),
            'inference_time': inference_time
        }

        # Adversarial evaluation
        X_test_adv = X_test + np.random.uniform(-epsilon, epsilon, X_test.shape)

        start_time = time.time()
        y_pred_adv = model.predict(X_test_adv)
        y_pred_proba_adv = model.predict_proba(X_test_adv)[:,1]
        adv_inference_time = time.time() - start_time

        adv_metrics = {
            'accuracy': accuracy_score(y_test, y_pred_adv),
            'roc_auc': roc_auc_score(y_test, y_pred_proba_adv),
            'f1': f1_score(y_test, y_pred_adv),
            'precision': precision_score(y_test, y_pred_adv),
            'recall': recall_score(y_test, y_pred_adv),
            'inference_time': adv_inference_time
        }

        return clean_metrics, adv_metrics

    except Exception as e:
        print(f"Error during evaluation: {str(e)}")
        raise

if __name__ == "__main__":
    data_path = '/content/drive/MyDrive/creditcard.csv'
    results = run_comparison(data_path)