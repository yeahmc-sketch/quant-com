#!/usr/bin/env python3
"""
DTK10 + 妙想模拟交易 — 每日自动调仓
========================================
流程: DTK10选股 → 通过妙想API下单 → 邮件通知
每天08:30执行（盘前）
"""
import os, sys, json, time, requests, smtplib, logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import numpy as np, pandas as pd, xgboost as xgb, tushare as ts
import pyarrow.parquet as pq

# ====== 配置 ======
MX_APIKEY = os.environ.get("MX_APIKEY", "mkt_pJ6hbURiUjhXao0EvrPQYcSsGhu2jUIgRkqBYxzhHKY")
MX_API_URL = "https://mkapi2.dfcfs.com/finskillshub"
TUSHARE_TOKEN = "2e50aa62898e603850c324723dbcf05fbb5fa671c6160d26e4593f41"
EMAIL_HOST, EMAIL_PORT = "smtp.qq.com", 465
EMAIL_USER, EMAIL_PASS = "18313835@qq.com", "ngrzdzjuhwfnbgbh"
EMAIL_TO = "18313835@qq.com"
LOG_FILE = r"C:\ML_STATION\LGBM_ML_Package\data\dtk10_trade.log"
POS_FILE = r"C:\ML_STATION\LGBM_ML_Package\data\dtk10_positions.json"
DATA_PATH = r"C:\ML_STATION\LGBM_ML_Package\data\fusion20_master.parquet"
HOLD_DAYS = 10

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s', encoding='utf-8')

K31 = ['avg_turnover_20','amihud_illiq_20d','neg_pb_cs','ep_cs','overnight_ret',
       'gap_up_ma_bias','holder_num_chg','main_pct_5d','intraday_ret','jump_vol_ratio',
       'main_pct_5d_sq','surge_efficiency','skewness_20d','margin_balance_growth_5d',
       'vol_breakout','momentum_12m','amount_surge','volume_momentum','momentum_3m',
       'momentum_1m','downside_vol_20d','intraday_volatility_5','margin_buy_ratio',
       'follow_up','neg_debt_ratio','burst_pattern','coil_amplitude','kurtosis_20d',
       'netprofit_yoy','gross_margin','asset_turn','sector_crowdedness']

def send_email(subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'], msg['To'], msg['Subject'] = EMAIL_USER, EMAIL_TO, subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT) as s:
            s.login(EMAIL_USER, EMAIL_PASS); s.send_message(msg)
    except Exception as e:
        logging.error(f"邮件失败: {e}")

def is_trading_day():
    """查Tushare交易日历"""
    ts.set_token(TUSHARE_TOKEN)
    today = datetime.now().strftime('%Y%m%d')
    cal = ts.pro_api().trade_cal(exchange='SSE', start_date=today, end_date=today)
    return bool(cal.iloc[0]['is_open']) if len(cal) > 0 else False

def load_positions():
    """加载持仓记录: {code: {buy_date, sell_target, score}}"""
    if os.path.exists(POS_FILE):
        with open(POS_FILE) as f:
            return json.load(f)
    return {}

def save_positions(pos):
    os.makedirs(os.path.dirname(POS_FILE), exist_ok=True)
    with open(POS_FILE, 'w') as f:
        json.dump(pos, f, indent=2)

def get_sell_target_date(buy_date_str, hold_days=HOLD_DAYS):
    """计算卖出目标日: buy_date后第hold_days个交易日"""
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    start = (datetime.strptime(buy_date_str, '%Y%m%d') + pd.Timedelta(days=1)).strftime('%Y%m%d')
    end = (datetime.strptime(buy_date_str, '%Y%m%d') + pd.Timedelta(days=30)).strftime('%Y%m%d')
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end)
    open_days = cal[cal['is_open'] == 1]['cal_date'].tolist()
    return open_days[hold_days - 1] if len(open_days) >= hold_days else None

def dtk10_pick():
    """DTK10选股：返回Top5股票代码(6位数字)"""
    logging.info("DTK10选股中...")
    ts.set_token(TUSHARE_TOKEN)
    
    factors_from_parquet = [f for f in K31 if f != 'sector_crowdedness']
    table = pq.read_table(DATA_PATH, columns=['ts_code','trade_date','close','amount'] + factors_from_parquet)
    df = table.to_pandas()
    df.columns = [c.lower() for c in df.columns]
    df['trade_date'] = df['trade_date'].astype(str)
    
    # 计算拥挤度
    basic = ts.pro_api().stock_basic(list_status='L', fields='ts_code,name,industry')
    ind_map = basic.set_index('ts_code')['industry'].to_dict()
    df['industry'] = df['ts_code'].map(ind_map)
    sv = df.groupby(['industry','trade_date'])['amount'].sum().reset_index()
    sv = sv.sort_values(['industry','trade_date'])
    g_sv = sv.groupby('industry')
    sv['vol_10'] = g_sv['amount'].transform(lambda s: s.rolling(10, min_periods=5).mean())
    sv['vol_60'] = g_sv['amount'].transform(lambda s: s.rolling(60, min_periods=30).mean())
    sv['sector_crowdedness'] = sv['vol_10'] / (sv['vol_60'] + 1)
    crowd_map = sv.set_index(['industry','trade_date'])['sector_crowdedness']
    df = df.set_index(['industry','trade_date'])
    df['sector_crowdedness'] = crowd_map
    df = df.reset_index()
    df['sector_crowdedness'] = df['sector_crowdedness'].fillna(1.0)
    
    g = df.groupby('ts_code')['close']
    df['_fwd_ret'] = g.transform(lambda s: s.shift(-5) / s - 1)
    df['_target'] = df['_fwd_ret'].groupby(df['trade_date']).rank(pct=True)
    df = df.dropna(subset=['_target'])
    
    X = np.nan_to_num(df[K31].values, nan=0, posinf=0, neginf=0).astype('float32')
    y = df['_target'].values.astype('float32')
    
    model = xgb.XGBRegressor(max_depth=5, learning_rate=0.02, n_estimators=500,
        subsample=0.7, colsample_bytree=0.6, reg_lambda=5.0, reg_alpha=5.0,
        min_child_weight=500, gamma=1.0, device='cuda', verbosity=0, random_state=42)
    model.fit(X, y, verbose=0)
    
    # 取最新日期
    latest = sorted(df['trade_date'].unique())[-1]
    day = df[df['trade_date'] == latest].copy()
    Xd = np.nan_to_num(day[K31].values, nan=0, posinf=0, neginf=0).astype('float32')
    day = day.copy()
    day['_score'] = model.predict(Xd)
    day = day.sort_values('_score', ascending=False)
    
    # 过滤
    day = day[~day['ts_code'].str.match(r'^(300|301|688|8)')]  # 排创业板/科创/北交
    day = day[day['close'] <= 60]  # 排60元以上
    # ST过滤
    st_codes = set(basic[basic['name'].str.contains('ST', na=False)]['ts_code'])
    day = day[~day['ts_code'].isin(st_codes)]
    
    picks = day.head(5)['ts_code'].tolist()
    picks_6digit = [c.split('.')[0] for c in picks]
    logging.info(f"DTK10 picks: {picks_6digit}")
    return picks_6digit, {c.split('.')[0]: s for c, s in zip(picks, day.head(5)['_score'])}

def mx_api(path, data):
    """调用妙想API"""
    headers = {"Content-Type": "application/json", "apikey": MX_APIKEY}
    url = f"{MX_API_URL}{path}"
    r = requests.post(url, json=data, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def mx_trade(code, action, quantity=100):
    """妙想模拟交易: 市价委托"""
    data = {"type": action, "stockCode": code, "quantity": quantity, "useMarketPrice": True}
    return mx_api("/api/claw/mockTrading/trade", data)

def mx_balance():
    """查询账户资金，返回 {initMoney, totalAssets, availBalance}(单位:元)"""
    r = mx_api("/api/claw/mockTrading/balance", {"moneyUnit": 1})
    d = r.get("data", {})
    unit = d.get("currencyUnit", 1000)
    return {
        "initMoney": d.get("initMoney", 0) / unit,
        "totalAssets": d.get("totalAssets", 0) / unit,
        "availBalance": d.get("availBalance", 0) / unit,
    }

def mx_positions():
    """查询当前持仓列表"""
    r = mx_api("/api/claw/mockTrading/positions", {"moneyUnit": 1})
    d = r.get("data", {})
    return d.get("posList") or []

def calc_position_size(cash, stock_code, num_positions):
    """等权分配: cash/num_positions/price, 向下取整到100股"""
    # 获取当前价格（用东财API或Tushare）
    import urllib.request, json
    mkt = "1" if stock_code.startswith("6") else "0"
    secid = f"{mkt}.{stock_code}"
    try:
        url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&fields1=f2&klt=1&lmt=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
        klines = data.get("data", {}).get("klines", [])
        if klines:
            price = float(klines[0].split(",")[2])
        else:
            price = 10.0  # fallback
    except:
        price = 10.0
    
    alloc = cash / num_positions
    lots = max(1, int(alloc / price / 100))
    return lots * 100

def mx_balance():
    """查询资金"""
    return mx_api("/api/claw/mockTrading/balance", {})

def run_daily():
    """每日主流程"""
    today = datetime.now().strftime('%Y-%m-%d')
    today_ymd = datetime.now().strftime('%Y%m%d')
    
    # --force-sell: 一键清仓（不受交易日限制）
    positions = load_positions()
    if '--force-sell' in sys.argv:
        logging.info(f"{today} --force-sell 强制清仓")
        for code in list(positions.keys()):
            try:
                mx_trade(code, "sell", quantity=positions[code].get("shares", 100))
                del positions[code]
                logging.info(f"  卖出 {code}")
            except Exception as e:
                logging.error(f"  卖出 {code} 失败: {e}")
        save_positions(positions)
        send_email(f"DTK10 🧹 {today} 强制清仓", "已清仓")
        return
    
    if not is_trading_day():
        logging.info(f"{today} 非交易日，跳过")
        return
    
    logging.info(f"{today} 交易日")
    
    # 检查是否有到期
    expired = []
    for code, info in list(positions.items()):
        if info.get('sell_target', '') <= today_ymd:
            expired.append(code)
    
    # 2. 判断是否需要调仓
    max_positions = 5
    need_rebalance = len(expired) > 0 or len(positions) < max_positions
    
    if not need_rebalance:
        logging.info(f"  无需调仓: 持仓{len(positions)}只, 无到期")
        return  # 不训练、不选股、不交易
    
    logging.info(f"  需要调仓: 到期{len(expired)}只, 持仓{len(positions)}只")
    
    # 3. 卖出到期持仓（全仓卖出）
    results = []
    for code in expired:
        # 查持仓数量
        pos_list = mx_positions()
        pos_qty = 100  # 默认
        for p in pos_list:
            if p.get("secCode") == code:
                pos_qty = p.get("availCount", 100)
                break
        
        logging.info(f"  到期卖出: {code} x{pos_qty}")
        try:
            r = mx_trade(code, "sell", quantity=pos_qty)
            results.append(f"到期卖出 {code} x{pos_qty}: {r.get('message','OK')}")
            del positions[code]
        except Exception as e:
            results.append(f"到期卖出 {code} 失败: {e}")
    
    # 4. 需要买入 → 查余额 + 等权分配
    slots = max_positions - len(positions)
    if slots <= 0:
        save_positions(positions)
        return
    
    # 查余额
    bal = mx_balance()
    cash = bal.get("availBalance", 100000)
    if cash < 5000:
        msg = f"余额不足: ¥{cash}"
        logging.warning(msg)
        send_email(f"DTK10 ⚠️ {today} 余额不足", msg)
        return
    
    # 训练+选股
    picks, scores = dtk10_pick()
    
    # 买入 — 每买完一只重新查余额
    bought_today = []
    for code in picks:
        if code in positions:
            continue
        if len(bought_today) >= slots:
            break
        # 重新查余额
        bal = mx_balance()
        remaining_cash = bal.get("availBalance", 0)
        remaining_slots = slots - len(bought_today)
        if remaining_cash < 5000 or remaining_slots <= 0:
            break
        qty = calc_position_size(remaining_cash, code, remaining_slots)
        if qty < 100:
            continue
        try:
            r = mx_trade(code, "buy", quantity=qty)
            results.append(f"买入 {code} x{qty}: {r.get('message','OK')}")
            sell_target = get_sell_target_date(today_ymd)
            positions[code] = {"buy_date": today_ymd, "sell_target": sell_target,
                               "shares": qty, "score": float(scores.get(code, 0))}
            bought_today.append(code)
            time.sleep(1)
        except Exception as e:
            results.append(f"买入 {code} 失败: {e}")
    
    save_positions(positions)
    
    # 6. 邮件通知
    held_info = "\n".join([f"  {c}: 买{p['buy_date']} x{p.get('shares','?')}股 到期{p['sell_target']}" 
                           for c, p in positions.items()])
    body = f"""DTK10 调仓报告 ({today})
══════════════════════
到期卖出: {len(expired)}只
新买入: {len(bought_today)}只

操作记录:
{chr(10).join(results) if results else '无变动'}

当前持仓 ({len(positions)}/5只 HD={HOLD_DAYS}天):
{held_info if held_info else '空仓'}

下次到期: {min([p['sell_target'] for p in positions.values()]) if positions else '无'}"""
    send_email(f"DTK10 🔄 {today} 调仓", body)
    logging.info(f"调仓: {len(expired)}卖 {len(bought_today)}买 → 持仓{len(positions)}只")

if __name__ == '__main__':
    run_daily()
