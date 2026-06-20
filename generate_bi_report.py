# -*- coding: utf-8 -*-
"""
天猫用户销售数据 BI 可视化报表
生成包含营收、产品、用户等多维度的交互式HTML报表
"""
import pymysql
import pandas as pd
from user_auth_db import get_connection  # 数据库连接配置，请修改 user_auth_db.py 中的密码
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import datetime
import json

# ============================================================
# 1. 数据提取
# ============================================================
def fetch_data():
    conn = get_connection()

    users = pd.read_sql("SELECT * FROM users", conn)
    orders = pd.read_sql("SELECT * FROM orders", conn)
    products = pd.read_sql("SELECT * FROM products", conn)
    behaviors = pd.read_sql("SELECT * FROM user_behaviors", conn)
    features = pd.read_sql("SELECT * FROM user_features", conn)

    conn.close()
    return users, orders, products, behaviors, features


def prepare_data(users, orders, products, behaviors, features):
    """数据预处理"""
    # 日期转换
    orders['order_date_dt'] = pd.to_datetime(orders['order_date_date'])
    orders['order_month'] = orders['order_date_dt'].dt.to_period('M').astype(str)
    orders['order_week'] = orders['order_date_dt'].dt.to_period('W').astype(str)

    behaviors['behavior_time_dt'] = pd.to_datetime(behaviors['behavior_time'])
    behaviors['behavior_date'] = behaviors['behavior_time_dt'].dt.date

    users['registration_dt'] = pd.to_datetime(users['registration_date'])

    # 合并订单与产品
    order_product = orders.merge(products, on='product_id', how='left')
    order_product['final_amount'] = order_product['actual_payment']

    return users, orders, products, behaviors, features, order_product


# ============================================================
# 2. 构建 BI 报表
# ============================================================
def build_bi_report(users, orders, products, behaviors, features, order_product):
    """构建完整BI报表HTML"""

    # ---- 营收KPI ----
    total_gmv = orders['total_amount'].sum() / 10000
    total_revenue = orders['actual_payment'].sum() / 10000
    total_orders = len(orders)
    avg_order_value = orders['actual_payment'].mean()
    discount_rate = (1 - orders['actual_payment'].sum() / orders['total_amount'].sum()) * 100
    unique_customers = orders['user_id'].nunique()
    repurchase_rate = (total_orders - unique_customers) / unique_customers * 100

    # ---- 月度营收趋势 ----
    monthly = orders.groupby('order_month').agg(
        GMV=('total_amount', 'sum'),
        revenue=('actual_payment', 'sum'),
        orders=('order_id', 'count'),
        customers=('user_id', 'nunique'),
        avg_order=('actual_payment', 'mean')
    ).reset_index()
    monthly['GMV'] /= 10000
    monthly['revenue'] /= 10000

    # ---- 日度营收趋势 ----
    daily = orders.groupby('order_date_date').agg(
        revenue=('actual_payment', 'sum'),
        orders=('order_id', 'count')
    ).reset_index()
    daily['revenue'] /= 10000
    daily['ma7'] = daily['revenue'].rolling(7).mean()

    # ---- 支付方式 ----
    payment_stats = orders.groupby('payment_method').agg(
        count=('order_id', 'count'),
        amount=('actual_payment', 'sum')
    ).reset_index()
    payment_stats['amount'] /= 10000

    # ---- 订单状态 ----
    status_stats = orders.groupby('order_status').agg(count=('order_id', 'count')).reset_index()

    # ---- 品类分析 ----
    category_stats = order_product.groupby('category').agg(
        sales=('final_amount', 'sum'),
        orders=('order_id', 'count'),
        products=('product_id', 'nunique'),
        avg_price=('unit_price', 'mean')
    ).reset_index()
    category_stats['sales'] /= 10000
    category_stats = category_stats.sort_values('sales', ascending=False)

    # ---- 品牌Top10 ----
    brand_stats = order_product.groupby('brand').agg(
        sales=('final_amount', 'sum'),
        orders=('order_id', 'count')
    ).reset_index()
    brand_stats['sales'] /= 10000
    brand_top10 = brand_stats.sort_values('sales', ascending=False).head(10)

    # ---- 用户画像 ----
    gender_stats = users['gender'].value_counts().reset_index()
    gender_stats.columns = ['gender', 'count']

    age_bins = [0, 18, 25, 30, 35, 40, 50, 100]
    age_labels = ['<18', '18-24', '25-29', '30-34', '35-39', '40-49', '50+']
    users['age_group'] = pd.cut(users['age'], bins=age_bins, labels=age_labels)

    age_stats = users['age_group'].value_counts().sort_index().reset_index()
    age_stats.columns = ['age_group', 'count']

    member_stats = users['member_level'].value_counts().reset_index()
    member_stats.columns = ['level', 'count']

    province_stats = users['province'].value_counts().head(10).reset_index()
    province_stats.columns = ['province', 'count']

    # ---- 消费等级 ----
    consumption_stats = features['consumption_level'].value_counts().reset_index()
    consumption_stats.columns = ['level', 'count']

    # ---- 行为漏斗 ----
    behavior_counts = behaviors['behavior_type'].value_counts()
    funnel_data = pd.DataFrame({
        'stage': ['浏览', '点击', '收藏', '加购', '下单'],
        'count': [
            behavior_counts.get('浏览', 0),
            behavior_counts.get('点击', 0),
            behavior_counts.get('收藏', 0),
            behavior_counts.get('加购', 0),
            total_orders
        ]
    })

    # ---- 周度行为与营收对比 ----
    daily_behaviors = behaviors.groupby('behavior_date').size().reset_index()
    daily_behaviors.columns = ['date', 'behavior_count']
    daily_behaviors['date'] = pd.to_datetime(daily_behaviors['date'])

    # ---- 评分分析 ----
    review_stats = orders[orders['review_score'].notna()]['review_score'].value_counts().sort_index().reset_index()
    review_stats.columns = ['score', 'count']

    # ---- 折后率趋势 ----
    monthly['discount_rate'] = (1 - monthly['revenue'] / monthly['GMV']) * 100

    # ---- 客单价趋势 ----
    monthly['arpu'] = monthly['revenue'] * 10000 / monthly['customers']

    # ============================================================
    # 月度数据预计算 (日期滑块用)
    # ============================================================
    def compute_monthly_data(orders, behaviors):
        months = sorted(orders['order_month'].unique())
        mdata = {'months': months}
        for key in ['gmv','revenue','orders','avg_order','discount_rate','arpu','unique_customers','dau_mean','stickiness']:
            mdata[key] = []
        for m in months:
            mo = orders[orders['order_month'] == m]
            bm = behaviors[behaviors['behavior_date'].between(
                pd.Timestamp(m + '-01').date(), (pd.Timestamp(m + '-01') + pd.offsets.MonthEnd(1)).date()
            )]
            mdata['gmv'].append(round(mo['total_amount'].sum()/10000,2))
            mdata['revenue'].append(round(mo['actual_payment'].sum()/10000,2))
            mdata['orders'].append(int(len(mo)))
            mdata['avg_order'].append(round(mo['actual_payment'].mean(),1) if len(mo)>0 else 0)
            dr = (1 - mo['actual_payment'].sum()/mo['total_amount'].sum())*100 if mo['total_amount'].sum()>0 else 0
            mdata['discount_rate'].append(round(dr,1))
            mdata['arpu'].append(round(mo['actual_payment'].sum()/mo['user_id'].nunique(),1) if mo['user_id'].nunique()>0 else 0)
            mdata['unique_customers'].append(int(mo['user_id'].nunique()))
            if len(bm)>0:
                dau_m = bm.groupby('behavior_date')['user_id'].nunique()
                mdata['dau_mean'].append(round(dau_m.mean(),0) if len(dau_m)>0 else 0)
                mdata['stickiness'].append(round(dau_m.mean()/bm['user_id'].nunique()*100,1) if bm['user_id'].nunique()>0 else 0)
            else:
                mdata['dau_mean'].append(0); mdata['stickiness'].append(0)
        return mdata


    def compute_sankey_data(orders, behaviors):
        ad = orders['order_date_dt'].dropna()
        mid = ad.min() + (ad.max()-ad.min())/2
        p1s,p1e = ad.min(),mid
        p2s,p2e = mid+pd.Timedelta(days=1),ad.max()
        per = {'P1':{'start':p1s,'end':p1e,'label':f'{p1s.strftime("%Y-%m-%d")} ~ {p1e.strftime("%Y-%m-%d")}'},
               'P2':{'start':p2s,'end':p2e,'label':f'{p2s.strftime("%Y-%m-%d")} ~ {p2e.strftime("%Y-%m-%d")}'}}
        def _lc(uid,ps,pe):
            uo=orders[(orders['user_id']==uid)&(orders['order_date_dt']>=ps)&(orders['order_date_dt']<=pe)]
            ub=behaviors[(behaviors['user_id']==uid)&(behaviors['behavior_time_dt']>=ps)&(behaviors['behavior_time_dt']<=pe)]
            ho=len(uo)>0; hb=len(ub)>0
            if ho:
                if uo['order_date_dt'].min()>=ps: return '新用户'
                elif hb and ub['behavior_time_dt'].max()>=pe-pd.Timedelta(days=30): return '活跃用户'
                else: return '沉默用户'
            elif hb:
                if ub['behavior_time_dt'].max()>=pe-pd.Timedelta(days=30): return '活跃用户'
                else: return '沉默用户'
            else: return '流失用户'
        def _rfm(uid,ps,pe):
            uo=orders[(orders['user_id']==uid)&(orders['order_date_dt']>=ps)&(orders['order_date_dt']<=pe)]
            if len(uo)==0: return '无消费'
            t=uo['actual_payment'].sum()
            if t>=500: return '高价值'
            elif t>=150: return '中价值'
            else: return '低价值'
        p1u=set(orders[(orders['order_date_dt']>=p1s)&(orders['order_date_dt']<=p1e)]['user_id'].unique())
        p2u=set(orders[(orders['order_date_dt']>=p2s)&(orders['order_date_dt']<=p2e)]['user_id'].unique())
        au=p1u|p2u
        p1lc={};p2lc={};p1r={};p2r={}
        for u in au:
            p1lc[u]=_lc(u,p1s,p1e); p2lc[u]=_lc(u,p2s,p2e)
            p1r[u]=_rfm(u,p1s,p1e); p2r[u]=_rfm(u,p2s,p2e)
        lcs=['新用户','活跃用户','沉默用户','流失用户']
        lm={s1:{s2:0 for s2 in lcs} for s1 in lcs}
        for u in au: lm[p1lc.get(u,'流失用户')][p2lc.get(u,'流失用户')]+=1
        lsrc=[];ltgt=[];lval=[];llbl=[f'P1-{s}' for s in lcs]+[f'P2-{s}' for s in lcs]
        for i,s1 in enumerate(lcs):
            for j,s2 in enumerate(lcs):
                if lm[s1][s2]>0: lsrc.append(i);ltgt.append(len(lcs)+j);lval.append(lm[s1][s2])
        rfs=['高价值','中价值','低价值','无消费']
        rm={s1:{s2:0 for s2 in rfs} for s1 in rfs}
        for u in au: rm[p1r.get(u,'无消费')][p2r.get(u,'无消费')]+=1
        rsrc=[];rtgt=[];rval=[];rlbl=[f'P1-{s}' for s in rfs]+[f'P2-{s}' for s in rfs]
        for i,s1 in enumerate(rfs):
            for j,s2 in enumerate(rfs):
                if rm[s1][s2]>0: rsrc.append(i);rtgt.append(len(rfs)+j);rval.append(rm[s1][s2])
        ln={}; lr={}
        for s in lcs:
            inflow=sum(lm[os][s] for os in lcs if os!=s)
            outflow=sum(lm[s][os] for os in lcs if os!=s)
            ln[s]=inflow-outflow
            tot=sum(lm[s].values())
            lr[s]=round(lm[s][s]/tot*100,1) if tot>0 else 0
        return {'periods':per,'lc_stages':lcs,'lc_matrix':lm,'lc_source':lsrc,'lc_target':ltgt,'lc_value':lval,'lc_labels':llbl,
            'rfm_stages':rfs,'rfm_matrix':rm,'rfm_source':rsrc,'rfm_target':rtgt,'rfm_value':rval,'rfm_labels':rlbl,
            'lc_net':ln,'lc_retention':lr,'total_users':len(au)}

    # 月度数据 (日期滑块用)
    mdata = compute_monthly_data(orders, behaviors)
    import json as _j
    mdata_json = _j.dumps(mdata, ensure_ascii=False, default=str)

    # 桑基图数据
    sdata = compute_sankey_data(orders, behaviors)

    # 3. 构建 HTML
    # ============================================================
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>天猫用户销售数据 BI 报表</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; background: #f0f2f5; color: #333; }}
.header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px 40px; }}
.header h1 {{ font-size: 28px; margin-bottom: 8px; }}
.header p {{ opacity: 0.85; font-size: 14px; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
.kpi-row {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 16px; margin-bottom: 24px; }}
.kpi-card {{ background: white; border-radius: 10px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); text-align: center; }}
.kpi-card .label {{ font-size: 13px; color: #888; margin-bottom: 6px; }}
.kpi-card .value {{ font-size: 26px; font-weight: bold; color: #333; }}
.kpi-card .value.green {{ color: #27ae60; }}
.kpi-card .value.blue {{ color: #2980b9; }}
.kpi-card .value.purple {{ color: #8e44ad; }}
.kpi-card .value.orange {{ color: #e67e22; }}
.kpi-card .value.red {{ color: #e74c3c; }}
.kpi-card .value.teal {{ color: #1abc9c; }}
.chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }}
.chart-row.full {{ grid-template-columns: 1fr; }}
.chart-row.three {{ grid-template-columns: 1fr 1fr 1fr; }}
.chart-card {{ background: white; border-radius: 10px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
.chart-card h3 {{ font-size: 16px; color: #555; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 2px solid #f0f2f5; }}
.chart-card .plot {{ width: 100%; }}
.section-title {{ font-size: 22px; color: #333; margin: 30px 0 16px 0; padding-left: 12px; border-left: 4px solid #667eea; }}

.time-filter {{ background: #fff; border-bottom: 1px solid #e8e8e8; padding: 14px 40px; display: flex; flex-wrap: wrap; gap: 14px; align-items: center; font-size: 13px; }}
.time-filter .filter-label {{ font-weight: 600; color: #555; white-space: nowrap; }}
.time-filter .slider-wrapper {{ flex: 1; min-width: 280px; position: relative; padding: 10px 0 4px; }}
.time-filter .slider-track {{ position: relative; height: 6px; background: #e0e0e0; border-radius: 3px; }}
.time-filter .slider-fill {{ position: absolute; height: 6px; background: linear-gradient(90deg, #667eea, #764ba2); border-radius: 3px; }}
.time-filter input[type="range"] {{ position: absolute; top: -8px; left: 0; width: 100%%; height: 6px; background: transparent; -webkit-appearance: none; pointer-events: none; margin: 0; padding: 0; z-index: 2; }}
.time-filter input[type="range"]::-webkit-slider-thumb {{ -webkit-appearance: none; pointer-events: all; width: 28px; height: 28px; background: #fff; border: 3px solid #667eea; border-radius: 50%%; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }}
.time-filter input[type="range"]::-webkit-slider-thumb:hover {{ transform: scale(1.2); }}
.time-filter .slider-labels {{ display: flex; justify-content: space-between; margin-top: 6px; font-size: 11px; color: #999; }}
.time-filter .slider-value {{ background: #667eea; color: #fff; padding: 4px 12px; border-radius: 4px; font-size: 12px; font-weight: 600; min-width: 90px; text-align: center; }}
.time-filter .btn-reset {{ padding: 6px 12px; background: #fff; color: #888; border: 1px solid #ddd; border-radius: 6px; cursor: pointer; font-size: 13px; white-space: nowrap; }}
.time-filter .btn-reset:hover {{ background: #f5f5f5; }}
.time-filter .filter-info {{ color: #aaa; font-size: 12px; white-space: nowrap; }}
.insight-box {{ background: #f8f9ff; border-left: 3px solid #667eea; padding: 12px 16px; margin: 12px 0; border-radius: 0 8px 8px 0; font-size: 13px; color: #666; }}
.data-table {{ width: 100%%; border-collapse: collapse; font-size: 12px; }}
.data-table th {{ background: #667eea; color: white; padding: 8px 6px; white-space: nowrap; }}
.data-table td {{ padding: 6px; text-align: center; border-bottom: 1px solid #eee; }}
.table-wrap {{ overflow-x: auto; }}
.footer {{ text-align: center; padding: 20px; color: #999; font-size: 12px; }}
</style>
</head>
<body>

<div class="header">
    <h1>📊 天猫用户销售数据 BI 报表</h1>
    <p>数据范围：{orders['order_date_dt'].min().strftime('%Y-%m-%d')} 至 {orders['order_date_dt'].max().strftime('%Y-%m-%d')} | 用户数：{unique_customers:,} | 订单数：{total_orders:,}</p>
</div>

<div class="time-filter">
    <span class="filter-label">&#x1F5C4; 日期范围筛选:</span>
    <span class="slider-value" id="slider-start-disp">--</span>
    <div class="slider-wrapper" id="slider-wrapper">
        <div class="slider-track"><div class="slider-fill" id="slider-fill"></div></div>
        <input type="range" id="range-start" min="0" max="0" value="0" step="1" oninput="onSliderChange()">
        <input type="range" id="range-end" min="0" max="0" value="0" step="1" oninput="onSliderChange()">
        <div class="slider-labels" id="slider-labels"></div>
    </div>
    <span class="slider-value" id="slider-end-disp">--</span>
    <button class="btn-reset" onclick="resetTimeFilter()">&#x21BA; 重置</button>
    <span class="filter-info" id="filter-info">当前: 全部时段</span>
</div>

<div class="container">

<!-- KPI Cards -->
<div class="kpi-row">
    <div class="kpi-card">
        <div class="label">总GMV（万元）</div>
        <div class="value blue" id="kpi-gmv">{total_gmv:,.0f}</div>
    </div>
    <div class="kpi-card">
        <div class="label">实际营收（万元）</div>
        <div class="value green" id="kpi-revenue">{total_revenue:,.0f}</div>
    </div>
    <div class="kpi-card">
        <div class="label">订单总数</div>
        <div class="value purple" id="kpi-orders">{total_orders:,}</div>
    </div>
    <div class="kpi-card">
        <div class="label">客单价（元）</div>
        <div class="value orange" id="kpi-aov">{avg_order_value:,.0f}</div>
    </div>
    <div class="kpi-card">
        <div class="label">总折扣率</div>
        <div class="value red" id="kpi-discount">{discount_rate:.1f}%</div>
    </div>
    <div class="kpi-card">
        <div class="label">复购率</div>
        <div class="value teal" id="kpi-repurchase">{repurchase_rate:.1f}%</div>
    </div>
</div>

<h2 class="section-title">📈 营收趋势分析</h2>

<div class="chart-row full">
    <div class="chart-card">
        <h3>月度营收 & GMV 趋势</h3>
        <div class="plot" id="chart_monthly_revenue"></div>
    </div>
</div>

<div class="chart-row full">
    <div class="chart-card">
        <h3>日度营收趋势（含7日移动平均）</h3>
        <div class="plot" id="chart_daily_revenue"></div>
    </div>
</div>

<div class="chart-row">
    <div class="chart-card">
        <h3>月度订单量与客单价</h3>
        <div class="plot" id="chart_monthly_orders"></div>
    </div>
    <div class="chart-card">
        <h3>月度折扣率变化</h3>
        <div class="plot" id="chart_discount_rate"></div>
    </div>
</div>

<h2 class="section-title">🛒 订单与支付分析</h2>

<div class="chart-row">
    <div class="chart-card">
        <h3>支付方式分布</h3>
        <div class="plot" id="chart_payment"></div>
    </div>
    <div class="chart-card">
        <h3>订单状态分布</h3>
        <div class="plot" id="chart_status"></div>
    </div>
</div>

<h2 class="section-title">📦 产品分析</h2>

<div class="chart-row full">
    <div class="chart-card">
        <h3>品类销售额排名</h3>
        <div class="plot" id="chart_category_sales"></div>
    </div>
</div>

<div class="chart-row">
    <div class="chart-card">
        <h3>品牌销售额 Top 10</h3>
        <div class="plot" id="chart_brand_top10"></div>
    </div>
    <div class="chart-card">
        <h3>品类客单价对比</h3>
        <div class="plot" id="chart_category_avgprice"></div>
    </div>
</div>

<h2 class="section-title">👥 用户画像分析</h2>

<div class="chart-row three">
    <div class="chart-card">
        <h3>性别分布</h3>
        <div class="plot" id="chart_gender"></div>
    </div>
    <div class="chart-card">
        <h3>年龄分布</h3>
        <div class="plot" id="chart_age"></div>
    </div>
    <div class="chart-card">
        <h3>会员等级分布</h3>
        <div class="plot" id="chart_member"></div>
    </div>
</div>

<div class="chart-row">
    <div class="chart-card">
        <h3>用户地域分布 Top 10</h3>
        <div class="plot" id="chart_province"></div>
    </div>
    <div class="chart-card">
        <h3>消费能力等级分布</h3>
        <div class="plot" id="chart_consumption"></div>
    </div>
</div>

<h2 class="section-title">🔄 用户行为分析</h2>

<div class="chart-row full">
    <div class="chart-card">
        <h3>用户行为转化漏斗</h3>
        <div class="plot" id="chart_funnel"></div>
    </div>
</div>

<div class="chart-row full">
    <div class="chart-card">
        <h3>日度行为量与营收趋势对比</h3>
        <div class="plot" id="chart_behavior_revenue"></div>
    </div>
</div>

<h2 class="section-title">⭐ 评价分析</h2>

<div class="chart-row">
    <div class="chart-card">
        <h3>评价分数分布</h3>
        <div class="plot" id="chart_review"></div>
    </div>
    <div class="chart-card">
        <h3>月度 ARPU 趋势</h3>
        <div class="plot" id="chart_arpu"></div>
    </div>
</div>

</div>

<div style="max-width:1400px;margin:0 auto;padding:0 20px;">
<h2 class="section-title" style="margin-top:30px;">&#x1F500; 用户分层迁移分析（桑葚图）</h2>
</div>

<div class="container">
<div class="insight-box"><strong>时段对比:</strong> P1 = {sdata["periods"]["P1"]["label"]} | P2 = {sdata["periods"]["P2"]["label"]} - 分析{sdata["total_users"]:,}名用户在时段间的分层迁移</div>

<div class="chart-row">
<div class="chart-card"><h3>生命周期阶段迁移流</h3><div class="plot" id="chart_sankey_lc"></div></div>
</div>
<div class="chart-row">
<div class="chart-card"><h3>RFM价值分层迁移流</h3><div class="plot" id="chart_sankey_rfm"></div></div>
</div>

<h3 class="section-title">&#x1F4CA; 迁移深度分析</h3>
<div class="insight-box"><strong>迁移成因:</strong> 1)新用户首单体验决定留存 2)活跃用户受竞品分流和品类周期影响沉入沉默 3)沉默用户缺乏有效召回最终流失 4)消费价值随购买频次自然衰减</div>
<div class="insight-box"><strong>运营建议 (P0):</strong> 建立流失预警模型，阶梯式回归券召回; <strong>(P1):</strong> 新用户阶梯任务+关联品类推荐提升升级率; <strong>(P2):</strong> VIP专属活动+高价值用户KOC培养</div>
</div>

<div class="footer">
    <p>天猫用户销售数据 BI 报表 | 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} | 数据来源: tmall_data</p>
</div>

<script>
'''

    # ---- Plotly Charts as JSON ----
    charts_js = ""

    # 月度营收趋势
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(name='GMV(万元)', x=monthly['order_month'], y=monthly['GMV'], marker_color='#a0c4ff'), secondary_y=False)
    fig.add_trace(go.Bar(name='实际营收(万元)', x=monthly['order_month'], y=monthly['revenue'], marker_color='#667eea'), secondary_y=False)
    fig.add_trace(go.Scatter(name='订单数', x=monthly['order_month'], y=monthly['orders'], mode='lines+markers', marker_color='#e74c3c', line=dict(width=2)), secondary_y=True)
    fig.update_layout(template='plotly_white', height=380, hovermode='x unified', legend=dict(orientation='h', y=1.12))
    fig.update_yaxes(title='金额(万元)', secondary_y=False)
    fig.update_yaxes(title='订单数', secondary_y=True)
    charts_js += f"Plotly.newPlot('chart_monthly_revenue', {fig.to_json()}, {{responsive: true}});\n"

    # 日度营收趋势
    fig = go.Figure()
    fig.add_trace(go.Scatter(name='日营收(万元)', x=daily['order_date_date'], y=daily['revenue'],
                             mode='markers+lines', marker=dict(size=4, color='#a0c4ff'), line=dict(width=1, color='#e0e0e0')))
    fig.add_trace(go.Scatter(name='7日移动平均', x=daily['order_date_date'], y=daily['ma7'],
                             mode='lines', line=dict(width=2.5, color='#e74c3c')))
    fig.update_layout(template='plotly_white', height=380, hovermode='x unified', legend=dict(orientation='h', y=1.12))
    fig.update_yaxes(title='营收(万元)')
    charts_js += f"Plotly.newPlot('chart_daily_revenue', {fig.to_json()}, {{responsive: true}});\n"

    # 月度订单量与客单价
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(name='订单量', x=monthly['order_month'], y=monthly['orders'], marker_color='#667eea'), secondary_y=False)
    fig.add_trace(go.Scatter(name='客单价(元)', x=monthly['order_month'], y=monthly['avg_order'], mode='lines+markers',
                             marker_color='#e67e22', line=dict(width=2)), secondary_y=True)
    fig.update_layout(template='plotly_white', height=380, hovermode='x unified', legend=dict(orientation='h', y=1.12))
    fig.update_yaxes(title='订单量', secondary_y=False)
    fig.update_yaxes(title='客单价(元)', secondary_y=True)
    charts_js += f"Plotly.newPlot('chart_monthly_orders', {fig.to_json()}, {{responsive: true}});\n"

    # 折扣率变化
    fig = go.Figure()
    fig.add_trace(go.Scatter(name='折扣率(%)', x=monthly['order_month'], y=monthly['discount_rate'],
                             mode='lines+markers', marker=dict(size=8, color='#e74c3c'), line=dict(width=2)))
    fig.add_hline(y=discount_rate, line_dash="dash", line_color="gray", annotation_text=f"均值: {discount_rate:.1f}%")
    fig.update_layout(template='plotly_white', height=380)
    fig.update_yaxes(title='折扣率(%)')
    charts_js += f"Plotly.newPlot('chart_discount_rate', {fig.to_json()}, {{responsive: true}});\n"

    # 支付方式
    colors_payment = ['#667eea','#764ba2','#e74c3c','#27ae60','#f39c12']
    fig = make_subplots(rows=1, cols=2, specs=[[{'type':'pie'}, {'type':'bar'}]])
    fig.add_trace(go.Pie(labels=payment_stats['payment_method'], values=payment_stats['count'],
                         marker=dict(colors=colors_payment), hole=0.4, textinfo='label+percent'), row=1, col=1)
    fig.add_trace(go.Bar(x=payment_stats['payment_method'], y=payment_stats['amount'],
                         marker=dict(color=colors_payment), text=payment_stats['amount'].round(0), textposition='outside'), row=1, col=2)
    fig.update_yaxes(title='交易金额(万元)', row=1, col=2)
    fig.update_layout(template='plotly_white', height=380, showlegend=False)
    charts_js += f"Plotly.newPlot('chart_payment', {fig.to_json()}, {{responsive: true}});\n"

    # 订单状态
    colors_status = ['#95a5a6','#e74c3c','#f39c12','#3498db','#2ecc71','#27ae60','#9b59b6']
    fig = go.Figure(go.Pie(labels=status_stats['order_status'], values=status_stats['count'],
                           marker=dict(colors=colors_status), hole=0.4, textinfo='label+percent'))
    fig.update_layout(template='plotly_white', height=380)
    charts_js += f"Plotly.newPlot('chart_status', {fig.to_json()}, {{responsive: true}});\n"

    # 品类销售额排名
    fig = go.Figure(go.Bar(
        y=category_stats['category'], x=category_stats['sales'],
        orientation='h', marker=dict(color=category_stats['sales'], colorscale='Blues'),
        text=category_stats['sales'].round(0), textposition='outside'
    ))
    fig.update_layout(template='plotly_white', height=420)
    fig.update_xaxes(title='销售额(万元)')
    charts_js += f"Plotly.newPlot('chart_category_sales', {fig.to_json()}, {{responsive: true}});\n"

    # 品牌Top10
    fig = go.Figure(go.Bar(x=brand_top10['brand'], y=brand_top10['sales'], marker_color='#667eea',
                           text=brand_top10['sales'].round(0), textposition='outside'))
    fig.update_layout(template='plotly_white', height=380)
    fig.update_xaxes(title='品牌')
    fig.update_yaxes(title='销售额(万元)')
    charts_js += f"Plotly.newPlot('chart_brand_top10', {fig.to_json()}, {{responsive: true}});\n"

    # 品类客单价
    cat_avg = category_stats.sort_values('avg_price', ascending=True)
    fig = go.Figure(go.Bar(y=cat_avg['category'], x=cat_avg['avg_price'], orientation='h',
                           marker=dict(color=cat_avg['avg_price'], colorscale='Oranges'),
                           text=cat_avg['avg_price'].round(0), textposition='outside'))
    fig.update_layout(template='plotly_white', height=380)
    fig.update_xaxes(title='平均单价(元)')
    charts_js += f"Plotly.newPlot('chart_category_avgprice', {fig.to_json()}, {{responsive: true}});\n"

    # 性别分布
    fig = go.Figure(go.Pie(labels=gender_stats['gender'], values=gender_stats['count'],
                           marker=dict(colors=['#667eea','#e74c3c']), hole=0.5, textinfo='label+percent'))
    fig.update_layout(template='plotly_white', height=300, showlegend=False)
    charts_js += f"Plotly.newPlot('chart_gender', {fig.to_json()}, {{responsive: true}});\n"

    # 年龄分布
    fig = go.Figure(go.Bar(x=age_stats['age_group'], y=age_stats['count'], marker_color='#764ba2',
                           text=age_stats['count'], textposition='outside'))
    fig.update_layout(template='plotly_white', height=300)
    charts_js += f"Plotly.newPlot('chart_age', {fig.to_json()}, {{responsive: true}});\n"

    # 会员等级
    colors_member = ['#95a5a6','#f39c12','#e74c3c','#3498db','#2ecc71']
    fig = go.Figure(go.Pie(labels=member_stats['level'], values=member_stats['count'],
                           marker=dict(colors=colors_member), hole=0.5, textinfo='label+percent'))
    fig.update_layout(template='plotly_white', height=300, showlegend=False)
    charts_js += f"Plotly.newPlot('chart_member', {fig.to_json()}, {{responsive: true}});\n"

    # 省份Top10
    fig = go.Figure(go.Bar(x=province_stats['province'], y=province_stats['count'], marker_color='#667eea',
                           text=province_stats['count'], textposition='outside'))
    fig.update_layout(template='plotly_white', height=380)
    charts_js += f"Plotly.newPlot('chart_province', {fig.to_json()}, {{responsive: true}});\n"

    # 消费等级
    level_colors = {'高': '#27ae60', '中': '#f39c12', '低': '#e74c3c'}
    colors = [level_colors.get(l, '#999') for l in consumption_stats['level']]
    fig = go.Figure(go.Pie(labels=consumption_stats['level'], values=consumption_stats['count'],
                           marker=dict(colors=colors), hole=0.5, textinfo='label+percent'))
    fig.update_layout(template='plotly_white', height=380, showlegend=False)
    charts_js += f"Plotly.newPlot('chart_consumption', {fig.to_json()}, {{responsive: true}});\n"

    # 漏斗图
    fig = go.Figure(go.Funnel(y=funnel_data['stage'], x=funnel_data['count'],
                              textinfo='value+percent initial', marker=dict(color=['#a0c4ff','#667eea','#764ba2','#e74c3c','#27ae60'])))
    fig.update_layout(template='plotly_white', height=380)
    charts_js += f"Plotly.newPlot('chart_funnel', {fig.to_json()}, {{responsive: true}});\n"

    # 行为与营收对比
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(name='日行为量', x=daily_behaviors['date'], y=daily_behaviors['behavior_count'],
                             mode='lines', line=dict(width=1.5, color='#667eea')), secondary_y=False)
    fig.add_trace(go.Scatter(name='日营收(万元)', x=daily['order_date_date'], y=daily['revenue'],
                             mode='lines', line=dict(width=1.5, color='#e74c3c')), secondary_y=True)
    fig.update_layout(template='plotly_white', height=380, hovermode='x unified', legend=dict(orientation='h', y=1.12))
    fig.update_yaxes(title='行为量', secondary_y=False)
    fig.update_yaxes(title='营收(万元)', secondary_y=True)
    charts_js += f"Plotly.newPlot('chart_behavior_revenue', {fig.to_json()}, {{responsive: true}});\n"

    # 评价分布
    fig = go.Figure(go.Bar(x=review_stats['score'].astype(int), y=review_stats['count'], marker_color='#f39c12',
                           text=review_stats['count'], textposition='outside'))
    fig.update_layout(template='plotly_white', height=380)
    fig.update_xaxes(title='评分', dtick=1)
    fig.update_yaxes(title='订单数')
    charts_js += f"Plotly.newPlot('chart_review', {fig.to_json()}, {{responsive: true}});\n"

    # 月度ARPU
    fig = go.Figure(go.Scatter(name='ARPU(元)', x=monthly['order_month'], y=monthly['arpu'],
                               mode='lines+markers', marker=dict(size=8, color='#8e44ad'), line=dict(width=2)))
    fig.update_layout(template='plotly_white', height=380)
    fig.update_yaxes(title='ARPU(元/人)')
    charts_js += f"Plotly.newPlot('chart_arpu', {fig.to_json()}, {{responsive: true}});\n"

    # ---- Sankey charts ----
    if sdata.get('lc_source'):
        fig = go.Figure(go.Sankey(arrangement='snap',
            node=dict(pad=15, thickness=20, line=dict(color='black', width=0.5), label=sdata['lc_labels'],
                color=['#667eea','#e74c3c','#f39c12','#95a5a6','#667eea','#e74c3c','#f39c12','#95a5a6']),
            link=dict(source=sdata['lc_source'], target=sdata['lc_target'], value=sdata['lc_value'],
                color=['rgba(102,126,234,'+str(0.2+v/max(sdata['lc_value'])*0.6)+')' for v in sdata['lc_value']])))
        fig.update_layout(template='plotly_white', height=400)
        charts_js += "Plotly.newPlot('chart_sankey_lc', " + fig.to_json() + ", {responsive: true});\n"
    if sdata.get('rfm_source'):
        fig = go.Figure(go.Sankey(arrangement='snap',
            node=dict(pad=15, thickness=20, line=dict(color='black', width=0.5), label=sdata['rfm_labels'],
                color=['#27ae60','#f39c12','#e74c3c','#95a5a6','#27ae60','#f39c12','#e74c3c','#95a5a6']),
            link=dict(source=sdata['rfm_source'], target=sdata['rfm_target'], value=sdata['rfm_value'],
                color=['rgba(39,174,96,'+str(0.2+v/max(sdata['rfm_value'])*0.6)+')' for v in sdata['rfm_value']])))
        fig.update_layout(template='plotly_white', height=400)
        charts_js += "Plotly.newPlot('chart_sankey_rfm', " + fig.to_json() + ", {responsive: true});\n"

    html += charts_js
    # ---- Embed slider JS + data ----
    html += '<script>var MONTHLY_DATA = ' + mdata_json + ';'
    html += r'''
var months = MONTHLY_DATA.months;
var nMonths = months.length;
document.getElementById('range-start').max = nMonths - 1;
document.getElementById('range-end').max = nMonths - 1;
document.getElementById('range-end').value = nMonths - 1;
var ld = document.getElementById('slider-labels'); ld.innerHTML = '';
for (var i = 0; i < nMonths; i++) { var s = document.createElement('span'); s.textContent = months[i]; s.style.fontSize = '10px'; ld.appendChild(s); }
updateSliderFill();
function updateSliderFill() {
    var s = parseInt(document.getElementById('range-start').value);
    var e = parseInt(document.getElementById('range-end').value);
    document.getElementById('slider-fill').style.left = (s/(nMonths-1))*100 + '%';
    document.getElementById('slider-fill').style.right = (100-(e/(nMonths-1))*100) + '%';
    document.getElementById('slider-start-disp').textContent = months[s];
    document.getElementById('slider-end-disp').textContent = months[e];
    if(s>e){document.getElementById('range-start').value=e;document.getElementById('range-end').value=s;}
}
function onSliderChange() {
    var s=parseInt(document.getElementById('range-start').value);
    var e=parseInt(document.getElementById('range-end').value);
    if(s>e){var tmp=s;s=e;e=tmp;document.getElementById('range-start').value=s;document.getElementById('range-end').value=e;}
    updateSliderFill(); applyTimeFilter();
}
function getFilteredMonths() { var s=parseInt(document.getElementById('range-start').value); var e=parseInt(document.getElementById('range-end').value); return months.slice(s,e+1); }
function sumRange(arr, f) { var t=0; for(var i=0;i<nMonths;i++){if(f.indexOf(months[i])>=0) t+=(arr[i]||0);} return t; }
function avgRange(arr, f) { var t=0,c=0; for(var i=0;i<nMonths;i++){if(f.indexOf(months[i])>=0){t+=(arr[i]||0);c++;}} return c>0?t/c:0; }
function applyTimeFilter() {
    var f=getFilteredMonths();
    var info=document.getElementById('filter-info');
    if(f.length===nMonths) info.textContent = '\u5f53\u524d: \u5168\u90e8\u65f6\u6bb5 ('+nMonths+'\u4e2a\u6708)';
    else info.textContent = '\u5f53\u524d: '+f[0]+' ~ '+f[f.length-1]+' ('+f.length+'\u4e2a\u6708)';
    var gmv=sumRange(MONTHLY_DATA.gmv,f), revenue=sumRange(MONTHLY_DATA.revenue,f), orders=sumRange(MONTHLY_DATA.orders,f);
    var aov=orders>0?revenue*10000/orders:0, dr=avgRange(MONTHLY_DATA.discount_rate,f);
    var el; el=document.getElementById('kpi-gmv'); if(el)el.textContent=gmv.toLocaleString('en-US',{maximumFractionDigits:0});
    el=document.getElementById('kpi-revenue'); if(el)el.textContent=revenue.toLocaleString('en-US',{maximumFractionDigits:0});
    el=document.getElementById('kpi-orders'); if(el)el.textContent=orders.toLocaleString('en-US',{maximumFractionDigits:0});
    el=document.getElementById('kpi-aov'); if(el)el.textContent=aov.toLocaleString('en-US',{maximumFractionDigits:0});
    el=document.getElementById('kpi-discount'); if(el)el.textContent=dr.toFixed(1)+'%';
    var uc=0; for(var i=0;i<nMonths;i++){if(f.indexOf(months[i])>=0)uc+=MONTHLY_DATA.unique_customers[i];}
    var rr=orders>uc?(orders/uc*100).toFixed(1):'0.0'; el=document.getElementById('kpi-repurchase'); if(el)el.textContent=rr+'%';
    var fg=[],fr=[],fo=[],fa=[],fd=[],fp=[];
    for(var i=0;i<nMonths;i++){if(f.indexOf(months[i])>=0){fg.push(MONTHLY_DATA.gmv[i]);fr.push(MONTHLY_DATA.revenue[i]);fo.push(MONTHLY_DATA.orders[i]);fa.push(MONTHLY_DATA.avg_order[i]);fd.push(MONTHLY_DATA.discount_rate[i]);fp.push(MONTHLY_DATA.arpu[i]);}}
    try{Plotly.update('chart_monthly_revenue',{x:[f,f,f],y:[fg,fr,fo]},{},[0,1,2]);}catch(e){}
    try{Plotly.update('chart_monthly_orders',{x:[f,f],y:[fo,fa]},{},[0,1]);}catch(e){}
    try{Plotly.update('chart_discount_rate',{x:[f,f],y:[fd,fp]},{},[0,1]);}catch(e){}
}
function resetTimeFilter() { document.getElementById('range-start').value=0; document.getElementById('range-end').value=nMonths-1; updateSliderFill(); applyTimeFilter(); }
</script>
</body>
</html>'''

    return html


if __name__ == '__main__':
    print("正在提取数据...")
    users, orders, products, behaviors, features = fetch_data()
    print("数据预处理...")
    users, orders, products, behaviors, features, order_product = prepare_data(
        users, orders, products, behaviors, features
    )
    print("构建BI报表...")
    html = build_bi_report(users, orders, products, behaviors, features, order_product)
    print("BI报表数据构建完成 (不输出HTML文件)")
