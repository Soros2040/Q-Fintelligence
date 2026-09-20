"""Specific-risk head derived from stock_finance.risk_head's frozen formula."""
import numpy as np
from sklearn.linear_model import SGDRegressor
from sklearn.preprocessing import StandardScaler


def fit_specific_risk(readout, labels, history_means, loadings, mask, *, seed):
    features, targets = [], []
    for x, y, center, loading, valid in zip(readout, labels, history_means, loadings, mask):
        valid = valid & np.isfinite(center) & np.isfinite(y)
        innovation = y[valid]-center[valid]
        factor, _, rank, _ = np.linalg.lstsq(loading[valid], innovation, rcond=None)
        if rank != 3:
            raise ValueError('RISK_TARGET_FACTOR_RANK')
        residual = innovation-loading[valid]@factor
        features.append(x[valid]); targets.append(residual**2)
    x, y = np.vstack(features), np.concatenate(targets)
    scaler = StandardScaler().fit(x)
    target_scale = max(float(y.mean()), 1e-8)
    model = SGDRegressor(loss='squared_error', penalty='l2', alpha=.01, max_iter=40, tol=None,
                         eta0=.001, learning_rate='invscaling', power_t=.25, random_state=seed, shuffle=True)
    model.fit(scaler.transform(x), y/target_scale)
    return model, scaler, target_scale


def predict_specific_risk(fit, readout):
    model, scaler, target_scale = fit
    values = np.maximum(model.predict(scaler.transform(readout))*target_scale, 1e-8)
    if not np.isfinite(values).all():
        raise ValueError('RISK_PREDICTION_FINITE')
    return values
