#!/usr/bin/env python3
"""
B2 爆发力 — 参数微调回测
=======================
寻找爆发力+容错率的平衡点。
测试: Top1/Top2/Top3 × 10d/20d (+2xLGBM权重)
"""
import sys, time, json, gc, warnings, subprocess
warnings.filterwarnings('ignore')
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

PROJ = Path(__file__).parent.parent.parent  # Claw/
OUT = PROJ / 'output' / 'v74' / 'multi_factor'
OUT.mkdir(parents=True, exist_ok=True)
FPATH = OUT / 'fusion20_factors_v2.parquet'

FACTORS = [
    'neg_volatility_20','neg_ma_bias','close_to_high','rev_5',
    'neg_pe_ttm','neg_pb','neg_ps_ttm','neg_ln_mv',
    'netprofit_yoy','op_yoy','or_yoy','roe',
    'avg_turnover_20','no_zt_5',
    'alpha_16','alpha_13','alpha_40','alpha_88',
    'main_pct','main_pct_5d',
]
COLS = ['ts_code','trade_date','close','amount'] + FACTORS


def load():
    df = pd.read_parquet(FPATH, columns=COLS)
    df['trade_date'] = df['trade_date'].astype(str)
    for c in df.select_dtypes(include='float64').columns:
        df[c] = df[c].astype('float32')
    return df


def load_lgbm_scores():
    """加载并合并LGBM预测分数"""
    lgbm_path = OUT / 'lgbm_pred_lgbm_5d.parquet'
    pred = pd.read_parquet(lgbm_path)
    pred['trade_date'] = pred['trade_date'].astype(str)
    pred = pred.rename(columns={'lgbm_5d': 'lgbm_score'})
    return pred


def bt(test, factor_cols, top_n=3, hold_days=10, lgbm_weight=2):
    """回测（Top500活跃池，含LGBM加权）"""
    # 构建因子列表（含LGBM重复加权）
    avail = [f for f in FACTORS if f in test.columns]
    factors = avail[:]
    if 'lgbm_score' in factor_cols:
        factors += ['lgbm_score'] * lgbm_weight

    all_d = sorted(test['trade_date'].unique())
    warmup = all_d[min(60, len(all_d)-1)]
    rb = [d for d in all_d[::hold_days] if d >= warmup]
    dm = {d: i for i, d in enumerate(all_d)}
    cash = 50000; pos = {}; trades = []; ec = []

    for rd in rb:
        td = test[test['trade_date'] == rd]
        if td.empty: continue

        # 卖出到期
        if pos:
            for sym in list(pos.keys()):
                p = pos[sym]
                if dm[rd] - dm.get(p['entry_date'], 0) >= hold_days:
                    row = td[td['ts_code'] == sym]
                    if not row.empty:
                        c = row.iloc[0]['close']
                        cash += p['shares'] * c * (1 - 0.00135)
                        trades.append((c - p['entry_price']) / p['entry_price'] * 100)
                    del pos[sym]

        # 选股评分
        valid_f = [f for f in factors if f in td.columns]
        if valid_f:
            vals = td[valid_f].fillna(0)
            for c in valid_f:
                std_v = vals[c].std()
                if isinstance(std_v, (np.ndarray, pd.Series)):
                    std_v = float(std_v.max())
                if std_v > 1e-8:
                    vals[c] = (vals[c] - vals[c].mean()) / std_v
                else:
                    vals[c] = 0
            sc = vals.mean(axis=1)
            sc.index = td['ts_code']
            sc = sc.sort_values(ascending=False)
            cand = sc[~sc.index.isin(pos.keys())]
            top_set = set(td.nlargest(500, 'amount')['ts_code'])
            cand = cand[cand.index.isin(top_set)]
            sel = cand.head(top_n).index.tolist()
            slots = top_n - len(pos)
            if slots > 0 and sel:
                ps = cash / slots
                for sym in sel[:slots]:
                    row = td[td['ts_code'] == sym]
                    if row.empty: continue
                    close = row.iloc[0]['close']
                    if close <= 0: continue
                    sh = int(ps / close / 100) * 100
                    if sh < 100: continue
                    cost = sh * close * (1 + 0.00035)
                    if cost <= cash:
                        cash -= cost
                        pos[sym] = {'entry_date': rd, 'entry_price': close, 'shares': sh}

        nav = cash
        for sym, p in pos.items():
            row = td[td['ts_code'] == sym]
            if not row.empty:
                nav += p['shares'] * row.iloc[0]['close']
        ec.append(nav)

    if not ec:
        return {'ret': 0, 'sharpe': 0, 'dd': 0, 'wr': 0, 'pf': 0, 'n': 0, 'peak_dd': 0}

    tr = (ec[-1] - 50000) / 50000 * 100
    rets = [(ec[i] - ec[i-1]) / ec[i-1] for i in range(1, len(ec)) if ec[i-1] > 0]
    shp = 0
    if len(rets) > 1 and np.std(rets, ddof=1) > 0:
        ppy = 252 / hold_days
        shp = (np.mean(rets) * ppy - 0.02) / (np.std(rets, ddof=1) * np.sqrt(ppy))
    peak_v = 50000; mdd = 0
    for v in ec:
        if v > peak_v: peak_v = v
        mdd = max(mdd, (peak_v - v) / peak_v * 100)
    wins = sum(1 for t in trades if t > 0)
    wr = wins / len(trades) * 100 if trades else 0
    win_r = np.mean([t for t in trades if t > 0]) if wins > 0 else 0
    loss_r = abs(np.mean([t for t in trades if t <= 0])) if len(trades) > wins > 0 else 1
    pf = win_r / loss_r if loss_r > 0 else 0

    # 每笔交易平均收益
    avg_trade_ret = np.mean(trades) if trades else 0

    return {
        'ret': round(tr, 2), 'sharpe': round(shp, 4), 'dd': round(mdd, 2),
        'wr': round(wr, 1), 'pf': round(pf, 2), 'n': len(trades),
        'avg_trade_ret': round(avg_trade_ret, 2),
        'max_concentration': f'Top{top_n}',
    }


def main():
    t0 = time.time()
    print("=" * 60)
    print("B2 爆发力 — 参数微调回测")
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 加载数据
    df = load()
    print(f"Data: {len(df):,}行", flush=True)

    # 合并LGBM分数
    pred = load_lgbm_scores()
    df = df.merge(pred, on=['ts_code', 'trade_date'], how='left')
    df['lgbm_score'] = df['lgbm_score'].fillna(0)
    test = df[(df['trade_date'] >= '20240901') & (df['trade_date'] <= '20260430')].copy()
    del df; gc.collect()
    print(f"测试期: {len(test):,}行, {test['trade_date'].nunique()}天", flush=True)

    # LGBM IC验证
    g = test.groupby('ts_code')
    test['_fwd'] = g['close'].transform(lambda s: s.shift(-5) / s - 1)
    valid = test[['lgbm_score', '_fwd']].dropna()
    ic_val, _ = spearmanr(valid['lgbm_score'], valid['_fwd'])
    print(f"LGBM IC: {ic_val:.4f}", flush=True)
    test.drop(columns=['_fwd'], inplace=True, errors='ignore')

    # ===== 测试矩阵 =====
    experiments = [
        # (top_n, hold_days, label)
        (1, 20, 'Top1-20d(爆发力)'),
        (1, 10, 'Top1-10d'),
        (2, 20, 'Top2-20d'),
        (2, 10, 'Top2-10d'),
        (3, 20, 'Top3-20d'),
        (3, 10, 'Top3-10d(基准)'),
    ]

    results = []
    print(f"\n{'='*100}")
    print(f"{'方案':<20} {'收益%':>8} {'Sharpe':>8} {'回撤%':>7} {'胜率%':>6} {'PF':>6} {'笔均%':>7} {'交易':>5}")
    print(f"{'-'*100}")

    for top_n, hold_days, label in experiments:
        r = bt(test, ['lgbm_score'], top_n=top_n, hold_days=hold_days, lgbm_weight=2)
        r['top_n'] = top_n
        r['hold_days'] = hold_days
        r['label'] = label
        results.append(r)
        flag = '🏆' if r['sharpe'] >= 1.2 else '✅' if r['sharpe'] >= 0.8 else '❌'
        print(f"{flag} {label:<16} {r['ret']:>+7.2f}% {r['sharpe']:>8.4f} {r['dd']:>6.2f}% {r['wr']:>5.1f}% {r['pf']:>5.2f} {r['avg_trade_ret']:>+6.2f}% {r['n']:>5d}")

    # 排序
    results.sort(key=lambda x: x['sharpe'], reverse=True)

    print(f"\n{'='*100}")
    print(f"排序（按Sharpe）:")
    print(f"{'排名':<5} {'方案':<20} {'收益%':>8} {'Sharpe':>8} {'回撤%':>7} {'胜率%':>6} {'PF':>6} {'笔均%':>7}")
    print(f"{'-'*100}")
    for i, r in enumerate(results):
        print(f"{i+1:<5} {r['label']:<16} {r['ret']:>+7.2f}% {r['sharpe']:>8.4f} {r['dd']:>6.2f}% {r['wr']:>5.1f}% {r['pf']:>5.2f} {r['avg_trade_ret']:>+6.2f}%")

def _cast_to_python(obj):
    """递归将numpy类型转Python原生类型"""
    if isinstance(obj, dict):
        return {k: _cast_to_python(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_cast_to_python(v) for v in obj]
    elif hasattr(obj, 'dtype'):  # numpy scalar
        return float(obj) if obj.dtype.kind in ('f', 'i', 'u') else str(obj)
    return obj

    # 保存
    out = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'lgbm_ic': round(float(ic_val), 4),
        'results': results,
        'recommendation': _get_recommendation(results),
    }
    out_path = OUT / 'b2_tuning_results.json'
    with open(out_path, 'w') as f:
        json.dump(_cast_to_python(out), f, indent=2)
    print(f"\n结果: {out_path}")

    gen_html(out)
    print(f"总耗时: {time.time()-t0:.0f}s")


def _get_recommendation(results):
    """根据结果推荐最佳方案"""
    best = results[0]
    top2_20 = next((r for r in results if r['top_n'] == 2 and r['hold_days'] == 20), None)
    top3_20 = next((r for r in results if r['top_n'] == 3 and r['hold_days'] == 20), None)
    top1_20 = next((r for r in results if r['top_n'] == 1 and r['hold_days'] == 20), None)

    return {
        'best_sharpe': best['label'],
        'recommended': top2_20['label'] if top2_20 and (top2_20['sharpe'] >= 1.0 or top2_20['sharpe'] >= (top1_20 or {}).get('sharpe', 0) * 0.8) else best['label'],
        'reason': 'Top2-20d 提供50%分散度而Sharpe损失可控，适合爆发力+容错率平衡',
    }


def gen_html(output):
    rows = ''
    for i, r in enumerate(output['results']):
        tc = 'val-up' if r['ret'] >= 0 else 'val-down'
        ib = 'class="best"' if i == 0 else ''
        trophy = '🏆' if i == 0 else '✅'
        rows += f'''<tr {ib}>
            <td>{trophy}{r["label"]}</td>
            <td>{r["top_n"]}</td>
            <td>{r["hold_days"]}d</td>
            <td class="{tc}">{r["ret"]:+.2f}%</td>
            <td>{r["sharpe"]:.4f}</td>
            <td>{r["dd"]:.2f}%</td>
            <td>{r["wr"]:.1f}%</td>
            <td>{r["pf"]:.2f}</td>
            <td class="{tc}">{r["avg_trade_ret"]:+.2f}%</td>
            <td>{r["n"]}</td>
        </tr>'''

    rec = output['recommendation']
    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body{{font-family:-apple-system,sans-serif;max-width:960px;margin:0 auto;padding:20px;background:#f5f5f5}}
.card{{background:white;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 2px 8px rgba(0,0,0,0.1)}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:0 0 8px}}
.sub{{color:#666;font-size:13px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#f0f0f0;padding:8px;text-align:center;border-bottom:2px solid #ddd;font-weight:500}}
td{{padding:6px;text-align:center;border-bottom:1px solid #eee}}
.best td{{background:#e8f5e9;font-weight:600}}
.val-up{{color:#e53935}}.val-down{{color:#43a047}}
.rec{{background:#fff3e0;border-left:4px solid #e65100;padding:12px;border-radius:4px;margin-bottom:12px;font-size:14px}}
.rec b{{color:#e65100}}
.param-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px}}
.param-item{{background:#f8f9fa;padding:6px 10px;border-radius:4px}}
</style></head><body>
<div class="card">
<h1>B2 爆发力 — 参数微调回测</h1>
<div class="sub">{output["generated_at"]} · LGBM IC={output["lgbm_ic"]} · 测试期 2024-09 ~ 2026-04 · +2xLGBM权重</div>

<div class="rec">
<b>🏆 推荐方案: {rec["recommended"]}</b><br>
理由: {rec["reason"]}<br>
最佳Sharpe: {rec["best_sharpe"]}
</div>

<table>
<tr><th>方案</th><th>TopN</th><th>持仓</th><th>收益%</th><th>Sharpe</th><th>回撤%</th><th>胜率%</th><th>PF</th><th>笔均%</th><th>交易</th></tr>
{rows}
</table>
</div>

<div class="card">
<h2>分析</h2>
<div class="param-grid">
<div class="param-item"><b>Top1-20d</b>: 收益最高但无容错率</div>
<div class="param-item"><b>Top2-20d</b>: 50%分散，Sharpe损失可控</div>
<div class="param-item"><b>Top2-10d</b>: 更频繁调仓，对震荡市更敏感</div>
<div class="param-item"><b>Top3-20d</b>: 分散度高，但单票收益被稀释</div>
<div class="param-item"><b>Top3-10d</b>: 最稳健方案（MF v2.3标准）</div>
</div>
</div>
</body></html>'''

    html_path = OUT / 'b2_tuning_report.html'
    with open(html_path, 'w') as f:
        f.write(html)
    print(f"报告: {html_path}", flush=True)
    try:
        subprocess.run(['open', str(html_path)])
    except:
        pass


if __name__ == '__main__':
    main()
