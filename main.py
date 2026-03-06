import os
import json
import time
import datetime
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from mootdx.quotes import Quotes
import warnings
warnings.filterwarnings('ignore')

# ==========================================
# ⚙️ 模拟实盘核心配置 (左侧伏击专属参数)
# ==========================================
INITIAL_CAPITAL = 1000000.0   # 初始本金 100 万
MAX_POSITION_PCT = 0.20       # 单只股票最大仓位 20%

# 左侧核心三要素 (请与您网格回测跑出的最优参数保持一致)
P1 = 8.0              # 上轨偏移率 (卖出用)
P2 = 9.0              # 下轨偏移率 (买入用)
BIAS_THRESH = 6.0     # 负乖离率阈值 (代表 <-6.0%)

PORTFOLIO_FILE = 'left_portfolio.json' # 左侧专属账户记忆文件
EXCEL_LIST = 'stock_list.xlsx'         # 股票池
HTML_OUTPUT = 'left_index.html'        # 左侧专属看板

# ==========================================
# 🧮 A股真实费率计算器
# ==========================================
def calc_buy_cost(price, shares):
    value = price * shares
    commission = max(5.0, value * 0.00025)
    transfer_fee = value * 0.00001
    return value + commission + transfer_fee, commission + transfer_fee

def calc_sell_revenue(price, shares):
    value = price * shares
    stamp_tax = value * 0.0005
    commission = max(5.0, value * 0.00025)
    transfer_fee = value * 0.00001
    total_fee = stamp_tax + commission + transfer_fee
    return value - total_fee, total_fee

# ==========================================
# 🧠 账户记忆管理
# ==========================================
def load_portfolio():
    if not os.path.exists(PORTFOLIO_FILE):
        init_data = {
            "initial_capital": INITIAL_CAPITAL,
            "cash": INITIAL_CAPITAL,
            "holdings": {},  
            "history": []    
        }
        with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
            json.dump(init_data, f, ensure_ascii=False, indent=4)
        return init_data
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_portfolio(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# ==========================================
# 📊 策略计算引擎 (100% 对齐 Left Side Backtest)
# ==========================================
def analyze_stock(stock_info, client):
    symbol = stock_info['code']
    try:
        # 获取近期 K 线数据
        df = client.bars(symbol=symbol, frequency=9, offset=100)
        if df is None or len(df) < 60: return None
            
        df.rename(columns={'datetime':'日期','open':'开盘','close':'收盘','high':'最高','low':'最低','vol':'成交量'}, inplace=True)
        for c in ['开盘', '收盘', '最高', '最低', '成交量']: df[c] = pd.to_numeric(df[c], errors='coerce')

        # 1. 核心轨道基础 VAR1 & MID
        df['VAR1'] = (df['收盘'] + df['最高'] + df['开盘'] + df['最低']) / 4
        df['MID'] = df['VAR1'].ewm(span=32, adjust=False).mean()
        df['UPPER'] = df['MID'] * (1 + P1 / 100.0)
        df['LOWER'] = df['MID'] * (1 - P2 / 100.0)
        
        # 2. 乖离率基础 MA20 & BIAS
        df['MA20'] = df['收盘'].rolling(20).mean()
        df['BIAS_VAL'] = (df['收盘'] - df['MA20']) / df['MA20'] * 100
        
        # 3. MACD趋势滤网
        df['DIF'] = df['收盘'].ewm(span=12, adjust=False).mean() - df['收盘'].ewm(span=26, adjust=False).mean()
        df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['UP_TREND'] = (df['DIF'] > 0) & (df['DEA'] > 0) & (df['DIF'] > df['DEA'])

        # 取当前最新一根和前一根K线
        curr = df.iloc[-1]
        prev = df.iloc[-2]

        # 涨跌停判定 (基于昨收价)
        pre_close = prev['收盘']
        limit_threshold = 19.8 if symbol.startswith('688') or symbol.startswith('30') else 9.8
        pct_change = (curr['收盘'] / pre_close - 1) * 100
        is_limit_up = pct_change >= limit_threshold
        is_limit_down = pct_change <= -limit_threshold

        # --- 买入条件判定 (B_伏击) ---
        bias_ok = curr['BIAS_VAL'] < -BIAS_THRESH
        b_cond1 = (curr['最低'] <= curr['LOWER']) and bias_ok
        b_cond2 = (curr['收盘'] > curr['开盘']) and ((curr['收盘'] - curr['最低']) > (curr['最高'] - curr['收盘']))
        buy_signal = b_cond1 and b_cond2

        # --- 卖出条件判定 (S_落袋) ---
        s_cond1 = curr['最高'] >= curr['UPPER']
        body = abs(curr['收盘'] - curr['开盘'])
        upper_shadow = curr['最高'] - max(curr['收盘'], curr['开盘'])
        s_cond2 = (curr['收盘'] < curr['开盘']) or (upper_shadow > body * 1.5)
        vol_shrink = curr['成交量'] < prev['成交量']
        
        sell_signal = s_cond1 and s_cond2 and vol_shrink and (not curr['UP_TREND'])

        return {
            'code': symbol,
            'name': stock_info['name'],
            'price': curr['收盘'],
            'low': curr['最低'],          # 必须回传，用于记录抄底防守线
            'bias_val': curr['BIAS_VAL'], # 必须回传，用于买入优先级排序
            'buy_signal': buy_signal,
            'sell_signal': sell_signal,
            'is_limit_up': is_limit_up,
            'is_limit_down': is_limit_down
        }
        
    except Exception:
        return None

# ==========================================
# 🌐 HTML 实时看板生成器
# ==========================================
def generate_dashboard(portfolio, current_market_data):
    today_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    holdings_value = 0.0
    holdings_html = ""
    for code, info in portfolio['holdings'].items():
        current_price = current_market_data.get(code, {}).get('price', info['buy_price']) 
        market_val = current_price * info['shares']
        holdings_value += market_val
        
        float_pnl = market_val - info['cost']
        float_pnl_pct = (market_val / info['cost'] - 1) * 100
        color_class = "text-danger" if float_pnl > 0 else "text-success" 
        
        # 显示防守线距现在的距离
        safe_dist = (current_price / info['buy_day_low'] - 1) * 100
        safe_str = f"距破位:{safe_dist:+.1f}%"
        
        holdings_html += f"""
        <tr>
            <td>{code}</td>
            <td>{info['name']}</td>
            <td>{info['shares']}</td>
            <td>¥{info['buy_price']:.2f}</td>
            <td>¥{current_price:.2f}</td>
            <td class="{color_class}">¥{float_pnl:.2f} ({float_pnl_pct:.2f}%)</td>
            <td><span class="badge bg-warning text-dark">防守价: ¥{info['buy_day_low']:.2f} ({safe_str})</span></td>
            <td>{info['buy_date']}</td>
        </tr>
        """
    if not holdings_html:
        holdings_html = "<tr><td colspan='8' class='text-center'>当前空仓，耐心等待千股暴跌的黄金坑...</td></tr>"

    total_assets = portfolio['cash'] + holdings_value
    total_pnl = total_assets - portfolio['initial_capital']
    total_return = (total_assets / portfolio['initial_capital'] - 1) * 100
    
    history_html = ""
    for record in reversed(portfolio['history']): 
        pnl_str = f"¥{record.get('pnl', 0):.2f}" if record['action'] == 'SELL' else "-"
        color = "danger" if record['action'] == 'BUY' else "success" # 绿色代表卖出落袋
        history_html += f"""
        <tr>
            <td>{record['time']}</td>
            <td><span class="badge bg-{color}">{record['action']}</span></td>
            <td>{record['code']} ({record['name']})</td>
            <td>¥{record['price']:.2f}</td>
            <td>{record['shares']}</td>
            <td>{pnl_str}</td>
            <td>{record['reason']}</td>
        </tr>
        """
    if not history_html:
        history_html = "<tr><td colspan='7' class='text-center'>暂无交易流水。</td></tr>"

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>左侧伏击中控台</title>
        <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
        <style>body {{background-color: #f8f9fa; font-family: 'Microsoft YaHei';}} .metric-value{{font-size:1.8rem; font-weight:bold;}} .up-red{{color:#dc3545!important;}} .down-green{{color:#198754!important;}}</style>
    </head>
    <body class="p-4">
        <h2 class="mb-4">📉 左侧伏击(Left-Side) 量化中控台 <small class="text-muted" style="font-size:1rem;">(更新时间: {today_str})</small></h2>
        <div class="alert alert-info" role="alert">
          当前核心运行参数：<strong>P1 (上轨) = {P1}%，P2 (下轨) = {P2}%，极端负乖离率 < -{BIAS_THRESH}%</strong>
        </div>
        
        <div class="row mb-4">
            <div class="col"><div class="card p-3 shadow-sm"><div class="text-muted">初始本金</div><div class="metric-value">¥{portfolio['initial_capital']:,.2f}</div></div></div>
            <div class="col"><div class="card p-3 shadow-sm"><div class="text-muted">可用资金</div><div class="metric-value">¥{portfolio['cash']:,.2f}</div></div></div>
            <div class="col"><div class="card p-3 shadow-sm"><div class="text-muted">当前总盈亏</div><div class="metric-value {'up-red' if total_pnl>0 else 'down-green'}">¥{total_pnl:,.2f}</div></div></div>
            <div class="col"><div class="card p-3 shadow-sm"><div class="text-muted">总收益率</div><div class="metric-value {'up-red' if total_return>0 else 'down-green'}">{total_return:.2f}%</div></div></div>
        </div>

        <div class="card mb-4 shadow-sm">
            <div class="card-header bg-dark text-white fw-bold">💼 当前实盘持仓 & 止损防守位监控</div>
            <table class="table table-hover mb-0"><thead class="table-light"><tr><th>代码</th><th>名称</th><th>持仓股数</th><th>买入价</th><th>最新价</th><th>浮动盈亏</th><th>防守底线</th><th>买入日期</th></tr></thead><tbody>{holdings_html}</tbody></table>
        </div>

        <div class="card shadow-sm">
            <div class="card-header bg-secondary text-white fw-bold">📜 实盘交易流水</div>
            <div style="max-height: 400px; overflow-y: auto;">
                <table class="table table-striped mb-0"><thead class="table-light sticky-top"><tr><th>时间</th><th>方向</th><th>股票</th><th>成交价</th><th>数量</th><th>平仓盈亏</th><th>触发原因</th></tr></thead><tbody>{history_html}</tbody></table>
            </div>
        </div>
    </body>
    </html>
    """
    with open(HTML_OUTPUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ HTML 看板已更新！(查看 {HTML_OUTPUT})")

# ==========================================
# 🚀 主程序入口 (交易撮合枢纽)
# ==========================================
if __name__ == '__main__':
    print("===========================================")
    print("📡 左侧伏击：实盘/模拟交易中枢已启动...")
    print("===========================================")
    
    today_date = datetime.date.today().strftime('%Y-%m-%d')
    now_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    portfolio = load_portfolio()
    
    meta_df = pd.read_excel(EXCEL_LIST, usecols=[0, 1])
    meta_df.columns = ['code', 'name']
    meta_df.dropna(subset=['code'], inplace=True)
    meta_df['code'] = meta_df['code'].astype(str).str.replace(r'\.0$', '', regex=True).str.zfill(6)
    stock_list = meta_df.to_dict('records')

    client = Quotes.factory(market='std', multithread=True, heartbeat=True)
    market_data = {}
    valid_buys = []
    
    print("🔍 正在扫描全市场恐慌盘与触轨信号...")
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {executor.submit(analyze_stock, stock, client): stock['code'] for stock in stock_list}
        for future in as_completed(futures):
            res = future.result()
            if res:
                market_data[res['code']] = res
                if res['buy_signal']:
                    valid_buys.append(res)
            time.sleep(0.01)

    # ==========================
    # 🛑 处理卖出 (剔除了超时强平，纯靠技术指标)
    # ==========================
    sold_codes = []
    for code, info in list(portfolio['holdings'].items()):
        if info['buy_date'] == today_date:
            continue # T+1 铁律
            
        current_data = market_data.get(code)
        if not current_data: continue
        
        if current_data['is_limit_down']:
            print(f"🔒 跌停锁死: {info['name']} ({code}) 触发风控，但无对手盘！")
            continue
            
        # 这里用自然日粗略模拟持有天数。回测里15日是交易日，实盘中如果您需要极端精确，可以改成 21 个自然日。
        days_held = (datetime.date.today() - datetime.datetime.strptime(info['buy_date'], '%Y-%m-%d').date()).days
        
        curr_price = current_data['price']
        sell_reason = ""
        
        # 1. 纪律止损：15天内跌破买入当日最低价
        if days_held <= 15 and curr_price < info['buy_day_low']:
            sell_reason = f"破位止损 (跌破抄底价 ¥{info['buy_day_low']:.2f})"
        # 2. 落袋为安：触碰上轨且出现见顶K线
        elif current_data['sell_signal']:
            sell_reason = "S_落袋 (触碰上轨阻力区)"
            
        # 注意：此处彻底删除了 days_held >= 30 的强平判定，与您要求的网格逻辑完全一致。

        if sell_reason:
            shares = info['shares']
            net_revenue, fees = calc_sell_revenue(curr_price, shares)
            
            portfolio['cash'] += net_revenue
            pnl = net_revenue - info['cost']
            
            portfolio['history'].append({
                'time': now_time, 'action': 'SELL', 'code': code, 'name': info['name'],
                'price': curr_price, 'shares': shares, 'fees': fees, 'pnl': pnl, 'reason': sell_reason
            })
            del portfolio['holdings'][code]
            sold_codes.append(code)
            print(f"💰 卖出触发: {info['name']} ({code}) - {sell_reason}, 盈亏: {pnl:.2f}")

    # ==========================
    # 🟢 处理买入 (BIAS 极值优先)
    # ==========================
    # 【核心一致性】：优先买入 BIAS 最负（跌得最狠、最偏离20日线）的股票
    valid_buys.sort(key=lambda x: x['bias_val'], reverse=False) 
    
    for stock in valid_buys:
        code = stock['code']
        if code in portfolio['holdings'] or code in sold_codes: continue
        
        if stock['is_limit_up']:
            print(f"🚫 涨停拒单: {stock['name']} 满足触底条件，但已封板无法买入！")
            continue   
            
        price = stock['price']
        min_lot = 200 if code.startswith('688') else 100
        
        max_money = min(portfolio['initial_capital'] * MAX_POSITION_PCT, portfolio['cash'])
        shares_to_buy = int(max_money // price)
        shares_to_buy = (shares_to_buy // min_lot) * min_lot 
        
        if shares_to_buy >= min_lot:
            total_cost, fees = calc_buy_cost(price, shares_to_buy)
            if portfolio['cash'] >= total_cost:
                portfolio['cash'] -= total_cost
                
                # 【核心一致性】：必须记录买入当天的 LOW (最低价)，作为左侧生命线
                portfolio['holdings'][code] = {
                    'name': stock['name'],
                    'shares': shares_to_buy,
                    'buy_price': price,
                    'buy_date': today_date,
                    'cost': total_cost,
                    'buy_day_low': stock['low']  # 关键记忆点
                }
                
                portfolio['history'].append({
                    'time': now_time, 'action': 'BUY', 'code': code, 'name': stock['name'],
                    'price': price, 'shares': shares_to_buy, 'fees': fees, 'reason': f"B_伏击 (乖离率:{stock['bias_val']:.1f}%)"
                })
                print(f"🔫 买入触发: {stock['name']} ({code}) - 乖离率极值抄底")

    save_portfolio(portfolio)
    generate_dashboard(portfolio, market_data)

    print("✅ 左侧伏击日内任务完成，正在退出...")
    os._exit(0)
