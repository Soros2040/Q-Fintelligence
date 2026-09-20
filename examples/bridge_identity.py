"""An independent six-asset objective identity example using only stdlib.

The upper-triangular Q convention differs from symmetric matrix storage:
off-diagonal Q entries are included once. Inputs are synthetic.
"""
from itertools import product
import json


def verify():
    n, k = 6, 3
    mu = [0.012, 0.009, 0.015, 0.006, 0.011, 0.007]
    factor = [0.025, 0.02, 0.03, 0.015, 0.022, 0.018]
    sigma = [[factor[i]*factor[j] + (0.002+i*0.0001 if i == j else 0.0)
              for j in range(n)] for i in range(n)]
    previous = [1/k]*k + [0.0]*(n-k)
    risk_lambda, cost, penalty = 1.0, 0.001, 1.0
    q = [[0.0]*n for _ in range(n)]
    for i in range(n):
        q[i][i] = (-mu[i]/k + risk_lambda*sigma[i][i]/k**2
                   + cost*(abs(1/k-previous[i])-abs(previous[i]))
                   + penalty*(1-2*k))
        for j in range(i+1, n):
            q[i][j] = 2*risk_lambda*sigma[i][j]/k**2 + 2*penalty
    offset = cost*sum(map(abs, previous)) + penalty*k*k
    h = [-q[i][i]/2 - sum(q[min(i,j)][max(i,j)] for j in range(n) if j != i)/4
         for i in range(n)]
    shift = offset + sum(q[i][i] for i in range(n))/2 + sum(q[i][j] for i in range(n) for j in range(i+1,n))/4
    errors, feasible, scored = [], [], []
    for x in product((0, 1), repeat=n):
        direct = (-sum(mu[i]*x[i] for i in range(n))/k
                  + risk_lambda*sum(x[i]*sigma[i][j]*x[j] for i in range(n) for j in range(n))/k**2
                  + cost*sum(abs(x[i]/k-previous[i]) for i in range(n))
                  + penalty*(sum(x)-k)**2)
        quadratic = offset + sum(q[i][j]*x[i]*x[j] for i in range(n) for j in range(i,n))
        z = [1-2*b for b in x]
        spin = shift + sum(h[i]*z[i] for i in range(n)) + sum(q[i][j]*z[i]*z[j]/4 for i in range(n) for j in range(i+1,n))
        errors.extend((abs(direct-quadratic),abs(direct-spin)))
        scored.append((direct,x))
        if sum(x) == k:
            feasible.append((direct,x))
    maximum_error = max(errors)
    if maximum_error > 1e-10 or len(feasible) != 20:
        raise AssertionError('Objective identity or feasible-state count failed')
    optimum, basket = min(scored)
    if sum(basket) != k:
        raise AssertionError('Illustrative penalty did not select a feasible minimum')
    return {'status':'PASS','input':'synthetic','states':len(scored),'feasible_states':len(feasible),
            'maximum_identity_error':maximum_error,'minimum_penalized_energy':optimum,
            'minimum_basket':list(basket),'penalty':penalty,'external_calls':0}


if __name__ == '__main__':
    print(json.dumps(verify(), indent=2))
