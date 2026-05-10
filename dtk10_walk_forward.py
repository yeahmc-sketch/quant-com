#!/usr/bin/env python3
"""
Walk-Forward backtest — XGBoost + DTK10 配置，2024起横向对比
===========================================================
对齐 dynamic_tk_v2：XGBoost, 2024+验证, DTK10配置
DTK10 = 动态TK(3-5) + HD=10 + CL2/3 + T+1开盘 + 涨跌停过滤
"""
import os, json, time, gc, warnings
from datetime import datetime
import numpy as np
import pandas as pd
import xgboost as xgb
from scipy.stats import spearmanr
import pyarrow.parquet as pq

warnings.filterwarnings("ignore")

LOG = r"C:\Users\Administrator\WorkBuddy\Claw\bt_elite_real_" + datetime.now().strftime("%H%M%S") + ".log"
OUT = r"C:\Users\Administrator\WorkBuddy\Claw\bt_elite_real_result.json"
PREPROC = "C:/ML_STATION/LGBM_ML_Package/data/bt_elite_real_preproc.parquet"

# ==================== DTK10 K31 纯因子集（公平对比）====================
ELITE_FACTORS = [
    'avg_turnover_20', 'amihud_illiq_20d', 'neg_pb_cs', 'ep_cs',
    'overnight_ret', 'gap_up_ma_bias', 'holder_num_chg',
    'main_pct_5d', 'intraday_ret', 'jump_vol_ratio', 'main_pct_5d_sq',
    'surge_efficiency', 'skewness_20d', 'margin_balance_growth_5d',
    'vol_breakout', 'momentum_12m', 'amount_surge', 'volume_momentum',
    'momentum_3m', 'momentum_1m', 'downside_vol_20d', 'intraday_volatility_5',
    'margin_buy_ratio', 'follow_up', 'neg_debt_ratio', 'burst_pattern',
    'coil_amplitude', 'kurtosis_20d', 'netprofit_yoy', 'gross_margin', 'asset_turn',
]
assert len(ELITE_FACTORS) == 31

# ==================== DTK10 参数 ====================
CAPITAL      = 50000.0
FORWARD      = 5
HOLD_DAYS    = 10           # HD=10
TK_FIXED     = 5             # 基准TK
TK_MIN       = 3             # 动态TK下限
TK_MAX       = 5             # 动态TK上限
VOL_WINDOW   = 20            # 波动率计算窗口
VOL_METHOD   = "percentile"  # 波动率分位数法
VOL_THRESHOLD = 0.5          # 分位数阈值
MIN_HOLD_TK  = 10            # TK最小保持天数
COMM_RATE    = 0.001         # 佣金 0.1%
STAMP_RATE   = 0.0005        # 印花税 0.05%
SLIPPAGE     = 0.002         # 滑点 0.2%
BUY_COST     = COMM_RATE + SLIPPAGE
SELL_COST    = COMM_RATE + STAMP_RATE + SLIPPAGE

def log(s):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(s) + "\n"); f.flush()
    print(s, flush=True)

log("=" * 60)
log(f"Elite Factor WF Backtest — XGBoost DTK10 ({len(ELITE_FACTORS)} factors) [2024+]")
log(f"XGBoost GPU | TK:3-5动态 | HD:{HOLD_DAYS} | CL2/3 | T+1+涨跌停+成本")
log(f"Start: {datetime.now()}")
t0 = time.time()

# ==================== Step1: 加载数据 ====================
log("Step1: Loading data...")
DATA = r"C:\ML_STATION\LGBM_ML_Package\data\fusion20_master.parquet"
table = pq.read_table(DATA, columns=["ts_code", "trade_date", "close", "open", "pct_chg"] + ELITE_FACTORS)
df = table.to_pandas()
df.columns = [c.lower() for c in df.columns]
df["trade_date"] = df["trade_date"].astype(str)
df["pct_chg"] = df["pct_chg"].fillna(0.0)

# Target: T日close -> T+5日close（训练目标，不变）
g = df.groupby("ts_code")["close"]
df["_fwd_ret"] = g.transform(lambda s: s.shift(-FORWARD) / s - 1)
df["_target"] = df["_fwd_ret"].groupby(df["trade_date"]).rank(pct=True)
df = df.dropna(subset=["_target"]).reset_index(drop=True)
log(f"  {len(df):,} rows after dropna")

# 构建 price_db: {date: {code: {open, close, pct_chg}}}
log("Step1b: Building price_db...")
price_db = {}
grouped = list(df.groupby("trade_date"))
for date_str, grp in grouped:
    pdict = {}
    for _, row in grp.iterrows():
        pdict[row["ts_code"]] = {
            "open": row["open"], "close": row["close"], "pct_chg": row["pct_chg"]
        }
    price_db[date_str] = pdict
all_dates = sorted(price_db.keys())
date_idx = {d: i for i, d in enumerate(all_dates)}
log(f"  {len(all_dates)} trading dates")

# 保存预计算数据
save_cols = ["ts_code", "trade_date", "_target", "_fwd_ret"] + ELITE_FACTORS
df[save_cols].to_parquet(PREPROC)
log(f"  Preproc saved: {PREPROC}")
del df, grouped; gc.collect()

# ==================== Step2: Walk-Forward 窗口（仅2024+验证）====================
log("Step2: Building windows (val >= 2024)...")
dates_sorted = sorted(set(pd.read_parquet(PREPROC, columns=["trade_date"])["trade_date"]))
n_dates = len(dates_sorted)
STEP, TRAIN_SIZE, WIN_SIZE = 60, 480, 60
all_windows = []
for i in range(TRAIN_SIZE, n_dates - WIN_SIZE, STEP):
    all_windows.append({
        "id": len(all_windows),
        "train_start": dates_sorted[i - TRAIN_SIZE],
        "train_end":   dates_sorted[i - 1],
        "val_start":   dates_sorted[i],
        "val_end":     dates_sorted[min(i + WIN_SIZE - 1, n_dates - 1)],
    })
# 只保留验证期从2024年开始的窗口
windows = [w for w in all_windows if w["val_start"] >= "20240101"]
for i, w in enumerate(windows):
    w["id"] = i
log(f"  {len(windows)} windows (filtered from {len(all_windows)}, val >= 2024)")

# ==================== 真实交易辅助函数 ====================
def is_limit_up(code, date_str):
    info = price_db.get(date_str, {}).get(code)
    if info is None:
        return True
    return info["pct_chg"] >= 9.0

def is_limit_down(code, date_str):
    info = price_db.get(date_str, {}).get(code)
    if info is None:
        return True
    return info["pct_chg"] <= -9.0

# ==================== Step3: Walk-Forward 逐窗口 ====================
log("Step3: Walk-Forward backtest...")
all_trades = []
global_daily = []   # 收集所有窗口的每日NAV
win_ics = []

for w in windows:
    wid = w["id"]
    log(f"\n[Win {wid+1}/{len(windows)}] "
        f"train {w['train_start']}~{w['train_end']} | "
        f"val {w['val_start']}~{w['val_end']}")

    # 加载数据
    df_all = pd.read_parquet(PREPROC)
    tr = df_all[(df_all["trade_date"] >= w["train_start"]) &
                (df_all["trade_date"] <= w["train_end"])].copy()
    vl = df_all[(df_all["trade_date"] >= w["val_start"]) &
                (df_all["trade_date"] <= w["val_end"])].copy()
    del df_all; gc.collect()

    if len(tr) < 1000 or len(vl) < 100:
        log(f"  SKIP: too few samples")
        continue

    # --- 训练 ---
    Xtr = np.nan_to_num(tr[ELITE_FACTORS].values, nan=0, posinf=0, neginf=0).astype("float32")
    ytr = tr["_target"].values.astype("float32")
    Xvl = np.nan_to_num(vl[ELITE_FACTORS].values, nan=0, posinf=0, neginf=0).astype("float32")
    yvl = vl["_target"].values.astype("float32")

    model = xgb.XGBRegressor(
        max_depth=5, learning_rate=0.02, n_estimators=500,
        subsample=0.7, colsample_bytree=0.6,
        reg_lambda=5.0, reg_alpha=5.0,
        min_child_weight=500, gamma=1.0,
        device="cuda", verbosity=0, random_state=42,
        early_stopping_rounds=20,
    )
    model.fit(Xtr, ytr, eval_set=[(Xvl, yvl)], verbose=0)

    pred = model.predict(Xvl)
    ic = float(spearmanr(pred, yvl)[0]) if not np.isnan(spearmanr(pred, yvl)[0]) else 0.0
    win_ics.append(ic)
    n_trees = 500
    try:
        if hasattr(model, 'get_booster'):
            n_trees = model.get_booster().best_iteration or 500
    except:
        pass
    log(f"  IC={ic:.4f} trees={n_trees}")

    # --- DTK10 真实交易模拟 ---
    vl = vl.reset_index(drop=True)
    val_dates = sorted(vl["trade_date"].unique())

    # 可交易日期（必须有T+1价格 + 卖出目标价）
    test_dates = sorted(set(
        d for d in val_dates
        if date_idx.get(d, 0) < len(all_dates) - HOLD_DAYS - 2
    ))

    cash = CAPITAL
    positions = []   # [{code, buy_price, alloc, sell_target}]
    cn = 0           # 连续亏损计数
    sc_val = 1.0     # ConsecLoss scale
    current_tk = TK_FIXED
    last_tk_change = -999
    daily_nav = []

    for di, today in enumerate(test_dates):
        ti = date_idx.get(today, -1)
        t1_day = all_dates[ti + 1] if ti >= 0 and ti + 1 < len(all_dates) else None
        if t1_day is None:
            continue

        # --- 动态TK计算（DTK10逻辑）---
        if di >= VOL_WINDOW and di - last_tk_change >= MIN_HOLD_TK:
            recent_nav = [d["nav"] for d in daily_nav[max(0, di - VOL_WINDOW):di]]
            if len(recent_nav) >= 10:
                recent_ret = [np.log(recent_nav[i] / recent_nav[i - 1]) for i in range(1, len(recent_nav))]
                vol = float(np.std(recent_ret))

                if VOL_METHOD == "percentile":
                    all_vol = []
                    for j in range(VOL_WINDOW, di):
                        nj = [d["nav"] for d in daily_nav[max(0, j - VOL_WINDOW):j]]
                        if len(nj) >= 10:
                            rj = [np.log(nj[k] / nj[k - 1]) for k in range(1, len(nj))]
                            all_vol.append(np.std(rj))
                    if len(all_vol) > 0:
                        pct = float((np.array(all_vol) <= vol).sum() / len(all_vol))
                        if pct > VOL_THRESHOLD:
                            new_tk = TK_MIN
                        elif pct > VOL_THRESHOLD - 0.2:
                            new_tk = (TK_MIN + TK_MAX) // 2
                        else:
                            new_tk = TK_MAX
                        if new_tk != current_tk:
                            current_tk = new_tk
                            last_tk_change = di

        TK = current_tk

        # --- 1. 卖出到期持仓 ---
        sp = 0.0
        kp = []
        for p in positions:
            if p["sell_target"] == today:
                code = p["code"]
                info = price_db.get(today, {}).get(code)

                if info is None:
                    if di + 1 < len(test_dates):
                        p["sell_target"] = test_dates[di + 1]
                    kp.append(p)
                    continue

                if is_limit_down(code, today):
                    if di + 1 < len(test_dates):
                        p["sell_target"] = test_dates[di + 1]
                    kp.append(p)
                    continue

                sell_price = info["close"] * (1 - SLIPPAGE)
                actual_ret = sell_price / p["buy_price"] - 1
                sp += p["alloc"] * (1 + actual_ret) * (1 - COMM_RATE - STAMP_RATE)

                all_trades.append({
                    "win": wid, "buy_date": p["buy_date"], "sell_date": today,
                    "code": code,
                    "net_ret": float(actual_ret * (1 - SELL_COST)),
                })
            else:
                kp.append(p)

        positions = kp
        cash += sp

        # --- 2. 日终市值 ---
        m2m = 0.0
        for p in positions:
            info = price_db.get(today, {}).get(p["code"])
            if info:
                m2m += p["alloc"] * info["close"] / p["buy_price"]
            else:
                m2m += p["alloc"]
        nav = cash + m2m

        # --- 3. ConsecLoss CL2/3 ---
        if di > 0 and daily_nav:
            cn = cn + 1 if nav / daily_nav[-1]["nav"] - 1 < -0.001 else 0
            if cn >= 3:
                for p in positions:
                    cash += p["alloc"]
                positions = []
                m2m = 0.0
                sc_val = 0.0
            elif cn >= 2:
                sc_val = 0.5
            else:
                sc_val = 1.0

        daily_nav.append({"win": wid, "date": today, "nav": nav, "TK": TK, "scale": sc_val})

        # --- 4. 买入（DTK10方式：缺仓时补买）---
        if sc_val <= 0 or t1_day not in price_db:
            continue

        slots = TK - len(positions)
        if slots <= 0:
            continue

        day_data = vl[vl["trade_date"] == today].copy()
        if len(day_data) < TK:
            continue

        Xd = np.nan_to_num(day_data[ELITE_FACTORS].values, nan=0, posinf=0, neginf=0).astype("float32")
        scores = model.predict(Xd)
        day_data = day_data.copy()
        day_data["_score"] = scores

        ranked = day_data.sort_values("_score", ascending=False)

        # 筛选可买入的（非涨停），扩大候选
        buy_list = []
        for _, stock in ranked.iterrows():
            code = stock["ts_code"]
            if is_limit_up(code, today):
                continue
            if t1_day not in price_db or code not in price_db[t1_day]:
                continue
            t1_open = price_db[t1_day][code]["open"]
            if t1_open is None or t1_open <= 0:
                continue
            buy_list.append((code, t1_open))
            if len(buy_list) >= slots * 5:
                break

        if not buy_list:
            continue

        # 每仓位分配 = nav / TK × sc_val，上限于现金
        alloc_per = nav / TK * sc_val
        if cash > 0 and slots > 0:
            alloc_per = min(alloc_per, cash * 0.95 / slots)

        sell_target_idx = ti + 1 + HOLD_DAYS
        sell_target = all_dates[sell_target_idx] if sell_target_idx < len(all_dates) else None

        n_bought = 0
        for code, buy_open in buy_list:
            if n_bought >= slots:
                break
            if sell_target is None or alloc_per <= 0:
                continue

            cost = alloc_per * BUY_COST
            actual_alloc = alloc_per - cost
            cash -= alloc_per

            positions.append({
                "code": code, "buy_price": buy_open, "buy_date": today,
                "alloc": actual_alloc, "sell_target": sell_target,
            })
            n_bought += 1

    # 窗口总结
    total_ret = (daily_nav[-1]["nav"] / CAPITAL - 1) if daily_nav else 0
    n_win_trades = len([t for t in all_trades if t['win']==wid])
    log(f"  Val ret={total_ret*100:.1f}% trades={n_win_trades}")

    # 收集每日NAV到全局（每个窗口独立，起始NAV=CAPITAL）
    if daily_nav:
        global_daily.extend(daily_nav)

log(f"\n{'=' * 60}")
log(f"Total trades: {len(all_trades)}")
log(f"Windows with IC: {len(win_ics)}")

# ==================== Step4: 计算绩效（从每日NAV，对齐bt_grid_open.py）====================
if global_daily:
    td = pd.DataFrame(all_trades)
    gd = pd.DataFrame(global_daily)
    log(f"Total trades: {len(td)}, daily nav points: {len(gd)}")

    # --- 按窗口链式构建连续NAV曲线（带日期）---
    win_info = []
    chained_daily = []
    chained_with_dates = []  # 用于月度分解

    running_nav = CAPITAL
    for wid in sorted(gd["win"].unique()):
        wd = gd[gd["win"] == wid].sort_values("date")
        if len(wd) < 2:
            continue

        # 计算该窗口内部每日收益率
        wd_nav = wd["nav"].values
        wd_dates = wd["date"].values
        win_start_nav = wd_nav[0]
        win_ret = wd_nav[-1] / win_start_nav - 1

        # 链入全局NAV：window内部的每日涨跌幅应用到running_nav上
        for i in range(1, len(wd_nav)):
            daily_chg = wd_nav[i] / wd_nav[i-1]
            running_nav *= daily_chg
            chained_daily.append(running_nav)
            chained_with_dates.append({"date": str(wd_dates[i]), "nav": running_nav})

        win_info.append({
            "win": int(wid),
            "n_trades": int(len(td[td["win"] == wid])),
            "ret": float(win_ret),
            "wr": float((td[td["win"] == wid]["net_ret"] > 0).mean()),
        })

    # --- 从链式NAV计算绩效 ---
    chained_ret = pd.Series(chained_daily).pct_change().dropna()
    fin_nav = chained_daily[-1]
    n_days = len(chained_daily)
    n_years = n_days / 250

    cagr = float((fin_nav / CAPITAL) ** (1 / n_years) - 1) if n_years > 0 else 0
    vol = float(chained_ret.std() * np.sqrt(250))
    sharpe_no = float(chained_ret.mean() / max(chained_ret.std(), 1e-10) * np.sqrt(250))
    dd = float((pd.Series(chained_daily) / pd.Series(chained_daily).cummax() - 1).min())
    wr = float((td["net_ret"] > 0).mean())

    log(f"\n{'=' * 45}")
    log(f"  精英因子({len(ELITE_FACTORS)}) - 真实交易回测 (T+1开盘+涨跌停+成本+ConsecLoss)")
    log(f"{'=' * 45}")
    log(f"  CAGR:        {cagr * 100:>8.2f}%")
    log(f"  Sharpe(NO):  {sharpe_no:>8.4f}")
    log(f"  MaxDD:       {dd * 100:>8.2f}%")
    log(f"  WinRate:     {wr * 100:>8.1f}%")
    log(f"  Avg NetRet:  {td['net_ret'].mean() * 100:>8.2f}%")
    log(f"  年化波动:    {vol * 100:>8.2f}%")
    log(f"  Mean IC:     {np.mean(win_ics):>8.4f}")
    log(f"  ICIR:        {np.mean(win_ics) / max(np.std(win_ics), 1e-10):>8.4f}")

    # 窗口逐期
    for wi in win_info:
        log(f"  Win{wi['win']:2d}: n={wi['n_trades']:3d} ret={wi['ret']*100:+.1f}% WR={wi['wr']*100:.0f}%")

    # ====== 月度收益分解 ======
    monthly_df = pd.DataFrame(chained_with_dates)
    if len(monthly_df) > 0:
        monthly_df["month"] = monthly_df["date"].str[:6]  # YYYYMM
        monthly_df["ym"] = monthly_df["date"].str[:7]      # YYYY-MM

        # 每月首尾NAV
        month_groups = monthly_df.groupby("month")
        monthly_ret = []
        for m, grp in month_groups:
            if len(grp) < 2:
                continue
            first_nav = grp["nav"].iloc[0]
            last_nav = grp["nav"].iloc[-1]
            ret = last_nav / first_nav - 1
            monthly_ret.append({
                "month": str(m), "label": str(grp["ym"].iloc[0]),
                "ret": float(ret), "start_nav": float(first_nav),
                "end_nav": float(last_nav),
            })

        log(f"\n{'=' * 50}")
        log(f"  月度收益分解")
        log(f"{'=' * 50}")
        log(f"  {'月份':<8s} {'收益':>8s} {'起始净值':>12s} {'期末净值':>12s}")

        annual_ret = {}
        for mr in monthly_ret:
            y = mr["month"][:4]
            log(f"  {mr['label']:<8s} {mr['ret']*100:>+7.2f}% {mr['start_nav']:>12.0f} {mr['end_nav']:>12.0f}")
            annual_ret[y] = annual_ret.get(y, 1.0) * (1 + mr["ret"])

        # 年度汇总
        log(f"\n  年度汇总:")
        for y in sorted(annual_ret.keys()):
            yret = annual_ret[y] - 1
            log(f"    {y}: {yret*100:+.1f}%")

    # ====== 每年度独立Sharpe ======
    log(f"\n{'=' * 50}")
    log(f"  每年度独立Sharpe（避免早期极端值虚高）")
    log(f"{'=' * 50}")
    log(f"  {'年份':<6s} {'收益':>8s} {'月均':>8s} {'波动':>8s} {'Sharpe(年)':>10s}")

    from collections import defaultdict
    by_year = defaultdict(list)
    for mr in monthly_ret:
        by_year[mr["month"][:4]].append(mr["ret"])

    annual_sharpes = {}
    for y in sorted(by_year.keys()):
        rets = by_year[y]
        if len(rets) < 3:
            continue
        total_ret = float(np.prod([1 + r for r in rets]) - 1)
        m_mean = float(np.mean(rets)) * 100
        m_std = float(np.std(rets)) * 100
        s = float(np.mean(rets) / max(np.std(rets), 1e-10) * np.sqrt(12))
        annual_sharpes[y] = s
        log(f"  {y:<6s} {total_ret*100:>+7.1f}% {m_mean:>+7.1f}% {m_std:>+7.1f}% {s:>+9.2f}")

    # 近期Sharpe (2023+)
    recent_ret = [mr["ret"] for mr in monthly_ret if mr["month"] >= "202301"]
    if len(recent_ret) > 6:
        rec_sharpe = float(np.mean(recent_ret) / max(np.std(recent_ret), 1e-10) * np.sqrt(12))
        rec_cagr = float((np.prod([1+r for r in recent_ret]))**(12/len(recent_ret)) - 1)
        log(f"\n  近期 {len(recent_ret)/12:.1f}年: CAGR={rec_cagr*100:.1f}%  Sharpe={rec_sharpe:.2f}")

    result = dict(
        cagr=cagr, sharpe_no=sharpe_no, maxdd=dd, winrate=wr,
        annual_vol=vol, n_factors=len(ELITE_FACTORS), n_windows=len(win_ics),
        n_trades=len(td), n_daily_points=n_days,
        mean_ic=float(np.mean(win_ics)),
        icir=float(np.mean(win_ics) / max(np.std(win_ics), 1e-10)),
        win_perf=win_info,
        monthly=monthly_ret if 'monthly_ret' in dir() else [],
    )
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    log(f"\nSaved: {OUT}")

log(f"\nTotal: {time.time() - t0:.0f}s")
