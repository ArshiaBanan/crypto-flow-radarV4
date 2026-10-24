from app.research_v07 import bootstrap_mean_ci, monte_carlo_drawdown, spearman

def test_spearman_direction():
    assert spearman([1,2,3],[1,4,9]) > 0.99

def test_bootstrap_is_bounded():
    x=bootstrap_mean_ci([1,-1,1,-1],iterations=200)
    assert x['n']==4 and x['lo'] <= x['mean'] <= x['hi']

def test_monte_carlo_nonnegative():
    x=monte_carlo_drawdown([1,-0.5,2,-1],iterations=200)
    assert x['median_max_drawdown'] >= 0 and x['p95_max_drawdown'] >= x['median_max_drawdown']
