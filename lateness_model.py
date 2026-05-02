import numpy as np
import pandas as pd
import sqlite3
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error

# ============================================================
# LAYER 1 — MATH UTILS
# ============================================================

def mse(actual, predicted):
    return mean_squared_error(actual, predicted)

def mae(actual, predicted):
    return mean_absolute_error(actual, predicted)

def compute_residuals(y_true, y_pred):
    return y_true - y_pred

# ============================================================
# LAYER 2 — SYMMETRIC DECISION TREE
# ============================================================

class SymmetricTree:
    def __init__(self, max_depth=4, min_samples_leaf=5):
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.splits = []
        self.leaf_values = {}
        self.leaf_variance = {}
        self.leaf_counts = {}

    def _best_split(self, X, residuals):
        best_gain = -np.inf
        best_feature = None
        best_threshold = None
        n = len(residuals)
        total_var = np.var(residuals) * n

        for feature_idx in range(X.shape[1]):
            unique_values = np.unique(X[:, feature_idx])
            thresholds = (unique_values[:-1] + unique_values[1:]) / 2.0
            for threshold in thresholds:
                left_mask = X[:, feature_idx] <= threshold
                right_mask = ~left_mask
                if left_mask.sum() < self.min_samples_leaf or right_mask.sum() < self.min_samples_leaf:
                    continue
                left_res, right_res = residuals[left_mask], residuals[right_mask]
                left_var = np.var(left_res) * len(left_res)
                right_var = np.var(right_res) * len(right_res)
                gain = total_var - (left_var + right_var)
                if gain > best_gain:
                    best_gain, best_feature, best_threshold = gain, feature_idx, threshold
        return best_feature, best_threshold

    def fit(self, X, residuals):
        n = len(residuals)
        paths = np.array([""] * n)
        for depth in range(self.max_depth):
            f_idx, thresh = self._best_split(X, residuals)
            if f_idx is None: break
            self.splits.append((f_idx, thresh))
            go_right = X[:, f_idx] > thresh
            paths = np.where(go_right, paths + "1", paths + "0")

        for path in np.unique(paths):
            mask = paths == path
            leaf_res = residuals[mask]
            self.leaf_values[path] = leaf_res.mean()
            self.leaf_variance[path] = leaf_res.var()
            self.leaf_counts[path] = int(mask.sum())
        return self

    def _route_samples(self, X):
        paths = np.array([""] * len(X))
        for f_idx, thresh in self.splits:
            paths = np.where(X[:, f_idx] > thresh, paths + "1", paths + "0")
        return paths

    def predict_with_variance(self, X):
        paths = self._route_samples(X)
        corrections, variances = np.zeros(len(X)), np.zeros(len(X))
        for i, path in enumerate(paths):
            if path in self.leaf_values:
                corrections[i], variances[i] = self.leaf_values[path], self.leaf_variance[path]
            else:
                for length in range(len(path) - 1, 0, -1):
                    if path[:length] in self.leaf_values:
                        corrections[i], variances[i] = self.leaf_values[path[:length]], self.leaf_variance[path[:length]]
                        break
        return corrections, variances

    def predict(self, X):
        c, _ = self.predict_with_variance(X)
        return c

# ============================================================
# LAYER 3 — GRADIENT BOOSTING
# ============================================================

class SimpleCatBoost:
    def __init__(self, n_estimators=100, learning_rate=0.1, max_depth=4, min_samples_leaf=5):
        self.n_estimators, self.learning_rate = n_estimators, learning_rate
        self.max_depth, self.min_samples_leaf = max_depth, min_samples_leaf
        self.trees, self.base_prediction = [], 0.0

    def fit(self, X, y, verbose=10):
        self.base_prediction = y.mean()
        preds = np.full(len(y), self.base_prediction)
        for i in range(self.n_estimators):
            res = compute_residuals(y, preds)
            tree = SymmetricTree(max_depth=self.max_depth, min_samples_leaf=self.min_samples_leaf).fit(X, res)
            self.trees.append(tree)
            preds += self.learning_rate * tree.predict(X)
            if verbose and (i + 1) % verbose == 0:
                print(f"  Round {i+1}/{self.n_estimators} - MAE: {mae(y, preds):.2f} min")
        return self

    def predict_with_interval(self, X, confidence=0.80):
        z = {0.80: 1.28, 0.90: 1.645, 0.95: 1.96}.get(confidence, 1.28)
        preds, total_var = np.full(len(X), self.base_prediction), np.zeros(len(X))
        for tree in self.trees:
            c, v = tree.predict_with_variance(X)
            preds += self.learning_rate * c
            total_var += (self.learning_rate ** 2) * v
        std_dev = np.sqrt(total_var)
        return np.round(preds, 1), np.round(preds - z * std_dev, 1), np.round(preds + z * std_dev, 1)

# ============================================================
# LAYER 4 — PREPROCESSOR
# ============================================================

class Preprocessor:
    def __init__(self, categorical_cols=None):
        self.categorical_cols = categorical_cols or []
        self.encoders = {col: LabelEncoder() for col in self.categorical_cols}

    def _expand_datetime(self, df, datetime_col):
        dt = pd.to_datetime(df[datetime_col])
        return pd.DataFrame({
            "hour": dt.dt.hour, "minute": dt.dt.minute, "day_of_week": dt.dt.dayofweek,
            "month": dt.dt.month, "day": dt.dt.day
        })

    def fit_transform(self, df, datetime_col, feature_cols, target_col):
        res = self._expand_datetime(df, datetime_col)
        for col in feature_cols:
            if col in self.categorical_cols:
                res[col] = self.encoders[col].fit_transform(df[col].astype(str))
            else: res[col] = df[col].values
        return res.values.astype(float), df[target_col].values.astype(float)

    def transform(self, df, datetime_col, feature_cols):
        res = self._expand_datetime(df, datetime_col)
        for col in feature_cols:
            if col in self.categorical_cols:
                known = set(self.encoders[col].classes_)
                safe = df[col].astype(str).apply(lambda x: x if x in known else self.encoders[col].classes_[0])
                res[col] = self.encoders[col].transform(safe)
            else: res[col] = df[col].values
        return res.values.astype(float)

# ============================================================
# LAYER 5 — DATABASE & MOCKING
# ============================================================

def load_mock_training_data():
    """Returns fake data to test logic without a real database."""
    data = []
    users = [("USER_A", "back in10", 5), ("USER_B", "goon", 15)]
    for uid, name, avg in users:
        for i in range(50):
            data.append({"time": "2026-04-10 09:00", "user_id": uid, "name": name, "minutes_late": avg + np.random.normal(0, 2)})
    return pd.DataFrame(data)

# ============================================================
# LAYER 6 — PIPELINE
# ============================================================

class LatenessPipeline:
    def __init__(self, use_mock=False):
        self.use_mock = use_mock
        self.categorical_cols = ["user_id", "name"]
        self.feature_cols = ["user_id", "name"]
        self.datetime_col, self.target_col = "time", "minutes_late"
        self.preprocessor = Preprocessor(categorical_cols=self.categorical_cols)
        self.model, self.trained = None, False

    def train(self, n_estimators=100):
        df = load_mock_training_data() if self.use_mock else self.load_real_data()
        X, y = self.preprocessor.fit_transform(df, self.datetime_col, self.feature_cols, self.target_col)
        self.model = SimpleCatBoost(n_estimators=n_estimators).fit(X, y)
        self.trained = True
        print("\nTraining Complete.")

    def load_real_data(self):
        conn = sqlite3.connect("events.db")
        query = "SELECT time, user_id, name, (CAST(lateness AS FLOAT)/60.0) as minutes_late FROM events WHERE lateness IS NOT NULL"
        df = pd.read_sql_query(query, conn)
        conn.close()
        return df

    def predict_with_confidence(self, user_id, event_name, event_time):
        if not self.trained: return None, None, None
        df = pd.DataFrame([{self.datetime_col: event_time, "user_id": user_id, "name": event_name}])
        X_new = self.preprocessor.transform(df, self.datetime_col, self.feature_cols)
        return self.model.predict_with_interval(X_new)

def setup_tables():
    """
    Ensures the database is ready for ML operations.
    This is called by main.py during the init_db process.
    """
    import sqlite3
    # Use the same DB name as your main bot
    conn = sqlite3.connect("events.db") 
    c = conn.cursor()
    
    # Example: A table to store user-specific AI preferences or offsets
    # You can leave this empty for now, or use it to ensure the 'events' 
    # table structure is exactly what the AI expects.
    c.execute('''CREATE TABLE IF NOT EXISTS ml_metadata 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    conn.commit()
    conn.close()
    print("ML specific tables verified.")
# ============================================================
# EXECUTION
# ============================================================

if __name__ == "__main__":
    print("Testing AI logic locally...")
    pipeline = LatenessPipeline(use_mock=True)
    pipeline.train()

    uid, ev, tm = "USER_A", "back in10", "2026-05-01 09:00"
    p, l, h = pipeline.predict_with_confidence(uid, ev, tm)
    
    print(f"\n[Test Result]")
    print(f"User: {uid} | Event: {ev}")
    print(f"Prediction: {p[0]} min late")
    print(f"80% Confidence Range: {l[0]} to {h[0]} min")