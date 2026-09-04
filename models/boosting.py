"""LightGBM-регрессор доходности на инженерных признаках."""
from __future__ import annotations

import numpy as np
import pandas as pd

from features import FEATURE_COLUMNS, NEWS_FEATURE_COLUMNS, TARGET


class LGBMReturnModel:
    """Прогноз будущей доходности по признакам; обучается на истории walk-forward."""

    name = "lgbm"

    def __init__(self, horizon: int = 1, seed: int = 42, use_news_features: bool = True):
        self.horizon = horizon
        self.seed = seed
        self.use_news_features = use_news_features
        self.model = None
        self.feature_importance_: pd.Series | None = None

    @property
    def feature_cols(self) -> list[str]:
        cols = list(FEATURE_COLUMNS)
        if self.use_news_features:
            cols += NEWS_FEATURE_COLUMNS
        return cols

    def _matrix(self, features: pd.DataFrame) -> np.ndarray:
        return features[self.feature_cols].to_numpy(dtype=np.float64)

    def fit(self, features: pd.DataFrame) -> "LGBMReturnModel":
        import lightgbm as lgb

        data = features.dropna(subset=self.feature_cols + [TARGET])
        X, y = self._matrix(data), data[TARGET].to_numpy(dtype=np.float64)
        self.model = lgb.LGBMRegressor(
            n_estimators=400,
            learning_rate=0.05,
            num_leaves=31,
            min_child_samples=20,
            subsample=0.9,
            subsample_freq=1,
            colsample_bytree=0.9,
            reg_lambda=1.0,
            random_state=self.seed,
            verbose=-1,
        )
        self.model.fit(X, y)
        self.feature_importance_ = pd.Series(
            self.model.feature_importances_, index=self.feature_cols
        ).sort_values(ascending=False)
        return self

    def predict_row(self, feature_row: pd.Series) -> float:
        if self.model is None:
            return 0.0
        X = np.asarray([feature_row[self.feature_cols].to_numpy(dtype=np.float64)])
        return float(self.model.predict(X)[0])

    def predict_all(self, features: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            return np.zeros(len(features))
        return self.model.predict(self._matrix(features))
