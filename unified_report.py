# -*- coding: utf-8 -*-
"""
天猫用户销售数据 - 统一分析报表 (Tab版)
整合营收、用户生命周期、产品、画像、行为、聚类、评价7大模块
"""
import pymysql
import pandas as pd
from user_auth_db import get_connection  # 数据库连接配置，请修改 user_auth_db.py 中的密码
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score, calinski_harabasz_score

# ============================================================
# SECTION 0: Data Extraction & Preparation
# ============================================================
def fetch_all_data():
    conn = get_connection()
    users = pd.read_sql("SELECT * FROM users", conn)
    orders = pd.read_sql("SELECT * FROM orders", conn)
    products = pd.read_sql("SELECT * FROM products", conn)
    behaviors = pd.read_sql("SELECT * FROM user_behaviors", conn)
    features = pd.read_sql("SELECT * FROM user_features", conn)
    conn.close()
    return users, orders, products, behaviors, features

def prepare_all_data(users, orders, products, behaviors, features):
    """统一数据预处理"""
    orders['order_date_dt'] = pd.to_datetime(orders['order_date_date'])
    orders['order_month'] = orders['order_date_dt'].dt.to_period('M').astype(str)
    orders['order_week'] = orders['order_date_dt'] - pd.to_timedelta(orders['order_date_dt'].dt.dayofweek, unit='d')
    orders['order_week'] = orders['order_week'].dt.date

    behaviors['behavior_time_dt'] = pd.to_datetime(behaviors['behavior_time'])
    behaviors['behavior_date'] = behaviors['behavior_time_dt'].dt.date
    behaviors['behavior_hour'] = behaviors['behavior_time_dt'].dt.hour
    behaviors['behavior_week'] = behaviors['behavior_time_dt'] - pd.to_timedelta(
        behaviors['behavior_time_dt'].dt.dayofweek, unit='d')
    behaviors['behavior_week'] = behaviors['behavior_week'].dt.date

    users['registration_dt'] = pd.to_datetime(users['registration_date'])

    order_product = orders.merge(products, on='product_id', how='left')
    return users, orders, products, behaviors, features, order_product


# ============================================================
# SECTION 1: Revenue & Overview Metrics (Tab 1)
# ============================================================
def compute_revenue_metrics(orders):
    total_gmv = orders['total_amount'].sum() / 10000
    total_revenue = orders['actual_payment'].sum() / 10000
    total_orders = len(orders)
    avg_order_value = orders['actual_payment'].mean()
    discount_rate = (1 - orders['actual_payment'].sum() / orders['total_amount'].sum()) * 100
    unique_customers = orders['user_id'].nunique()
    repurchase = (total_orders - unique_customers) / unique_customers * 100

    monthly = orders.groupby('order_month').agg(
        GMV=('total_amount', 'sum'), revenue=('actual_payment', 'sum'),
        orders=('order_id', 'count'), customers=('user_id', 'nunique'),
        avg_order=('actual_payment', 'mean')
    ).reset_index()
    monthly['GMV'] /= 10000
    monthly['revenue'] /= 10000
    monthly['discount_rate'] = (1 - monthly['revenue'] / monthly['GMV']) * 100
    monthly['arpu'] = monthly['revenue'] * 10000 / monthly['customers']

    # Month-over-month growth
    monthly['revenue_mom'] = monthly['revenue'].pct_change() * 100
    last_mom = monthly['revenue_mom'].iloc[-1] if len(monthly) > 1 else 0

    daily = orders.groupby('order_date_date').agg(
        revenue=('actual_payment', 'sum'), orders=('order_id', 'count')
    ).reset_index()
    daily['revenue'] /= 10000
    daily['ma7'] = daily['revenue'].rolling(7).mean()

    payment = orders.groupby('payment_method').agg(
        count=('order_id', 'count'), amount=('actual_payment', 'sum')
    ).reset_index()
    payment['amount'] /= 10000

    status = orders.groupby('order_status').size().reset_index(name='count')

    return {
        'total_gmv': total_gmv, 'total_revenue': total_revenue,
        'total_orders': total_orders, 'avg_order_value': avg_order_value,
        'discount_rate': discount_rate, 'unique_customers': unique_customers,
        'repurchase': repurchase, 'last_mom': last_mom,
        'monthly': monthly, 'daily': daily, 'payment': payment, 'status': status
    }


# ============================================================
# SECTION 2: User Lifecycle Metrics (Tab 2) - NEW
# ============================================================
def compute_user_lifecycle(users, orders, behaviors):
    metrics = {}
    ref_date = pd.Timestamp('2026-03-05')

    # --- DAU/WAU/MAU ---
    daily_active = behaviors.groupby('behavior_date')['user_id'].nunique().reset_index()
    daily_active.columns = ['date', 'dau']
    daily_active['date'] = pd.to_datetime(daily_active['date'])

    # WAU
    behaviors['week_num'] = behaviors['behavior_time_dt'].dt.isocalendar().year.astype(str) + '-W' + \
                             behaviors['behavior_time_dt'].dt.isocalendar().week.astype(str)
    wau_data = behaviors.groupby('week_num')['user_id'].nunique().reset_index()
    wau_data.columns = ['week', 'wau']

    # MAU
    monthly_active = behaviors.groupby(behaviors['behavior_time_dt'].dt.to_period('M').astype(str))['user_id'].nunique()
    mau_data = monthly_active.reset_index()
    mau_data.columns = ['month', 'mau']

    # Stickiness (avg DAU / MAU)
    avg_dau = daily_active['dau'].mean()
    last_mau = mau_data['mau'].iloc[-1]
    stickiness = avg_dau / last_mau * 100 if last_mau > 0 else 0

    metrics['daily_active'] = daily_active
    metrics['wau_data'] = wau_data
    metrics['mau_data'] = mau_data
    metrics['stickiness'] = stickiness

    # --- New vs Lost Users ---
    # First order date per user
    first_order = orders.groupby('user_id')['order_date_dt'].min().reset_index()
    first_order.columns = ['user_id', 'first_order_date']
    first_order['first_month'] = first_order['first_order_date'].dt.to_period('M').astype(str)

    last_order = orders.groupby('user_id')['order_date_dt'].max().reset_index()
    last_order.columns = ['user_id', 'last_order_date']

    # New users per month
    new_users_monthly = first_order.groupby('first_month').size().reset_index(name='new_users')

    # Lost users: last order > 30 days before ref_date
    last_order['days_since_last'] = (ref_date - last_order['last_order_date']).dt.days
    lost_users = last_order[last_order['days_since_last'] > 30]
    lost_users['lost_month'] = (ref_date - pd.Timedelta(days=30)).strftime('%Y-%m')
    lost_monthly = lost_users.groupby('lost_month').size().reset_index(name='lost_users')

    # Active users (ordered within 30 days)
    active_now = len(last_order[last_order['days_since_last'] <= 30])
    dormant = len(last_order[(last_order['days_since_last'] > 30) & (last_order['days_since_last'] <= 90)])
    churned = len(last_order[last_order['days_since_last'] > 90])

    metrics['new_users_monthly'] = new_users_monthly
    metrics['lost_monthly'] = lost_monthly
    metrics['lifecycle_dist'] = {'活跃用户': active_now, '沉默用户': dormant, '流失用户': churned}

    # --- Cohort Retention ---
    # Weekly retention after first order
    order_users = orders[['user_id', 'order_date_dt']].drop_duplicates()
    order_users = order_users.merge(first_order[['user_id', 'first_order_date']], on='user_id')
    order_users['week_from_first'] = ((order_users['order_date_dt'] - order_users['first_order_date']).dt.days / 7).astype(int)

    # Build cohort: users by first_order_week
    order_users['cohort_week'] = order_users['first_order_date'].dt.to_period('W').astype(str)
    cohort_sizes = order_users.groupby('cohort_week')['user_id'].nunique()

    retention_matrix = {}
    for week_num in range(0, 13):
        week_users = order_users[order_users['week_from_first'] == week_num].groupby('cohort_week')['user_id'].nunique()
        for cohort, count in week_users.items():
            if cohort not in retention_matrix:
                retention_matrix[cohort] = {}
            size = cohort_sizes.get(cohort, 1)
            retention_matrix[cohort][week_num] = round(count / size * 100, 1)

    # Convert to DataFrame for heatmap
    retention_rows = []
    for cohort, weeks in sorted(retention_matrix.items()):
        row = {'cohort': str(cohort), 'size': int(cohort_sizes.get(cohort, 0))}
        row.update({f'W{w}': weeks.get(w, 0) for w in range(13)})
        retention_rows.append(row)
    retention_df = pd.DataFrame(retention_rows).tail(10)  # last 10 cohorts

    metrics['retention_df'] = retention_df

    # --- Repurchase Interval ---
    user_orders_sorted = orders[['user_id', 'order_date_dt']].sort_values(['user_id', 'order_date_dt'])
    user_orders_sorted['prev_order'] = user_orders_sorted.groupby('user_id')['order_date_dt'].shift(1)
    user_orders_sorted['interval_days'] = (user_orders_sorted['order_date_dt'] - user_orders_sorted['prev_order']).dt.days
    repurchase_intervals = user_orders_sorted['interval_days'].dropna()
    repurchase_intervals = repurchase_intervals[repurchase_intervals > 0]

    metrics['repurchase_intervals'] = repurchase_intervals
    metrics['avg_repurchase_interval'] = repurchase_intervals.mean()

    # --- LTV ---
    user_ltv = orders.groupby('user_id')['actual_payment'].sum().reset_index()
    user_ltv.columns = ['user_id', 'ltv']
    metrics['avg_ltv'] = user_ltv['ltv'].mean()
    metrics['median_ltv'] = user_ltv['ltv'].median()

    # LTV by order count tier
    user_order_count = orders.groupby('user_id').size().reset_index(name='order_count')
    user_ltv = user_ltv.merge(user_order_count, on='user_id')
    ltv_by_orders = user_ltv.groupby('order_count')['ltv'].mean().reset_index()
    metrics['ltv_by_orders'] = ltv_by_orders

    return metrics


# ============================================================
# SECTION 3: Product Metrics (Tab 3)
# ============================================================
def compute_product_metrics(order_product, products, orders):
    category = order_product.groupby('category').agg(
        sales=('actual_payment', 'sum'), orders=('order_id', 'count'),
        products=('product_id', 'nunique'), avg_price=('price', 'mean'),
        avg_discount=('discount', 'mean')
    ).reset_index()
    category['sales'] /= 10000
    category = category.sort_values('sales', ascending=False)

    brand = order_product.groupby('brand').agg(
        sales=('actual_payment', 'sum'), orders=('order_id', 'count')
    ).reset_index()
    brand['sales'] /= 10000
    brand_top10 = brand.sort_values('sales', ascending=False).head(10)

    # Price distribution & sales count
    products['price_range'] = pd.cut(products['price'], bins=[0, 50, 100, 200, 500, 1000, 3000, 10000],
                                     labels=['0-50', '50-100', '100-200', '200-500', '500-1000', '1000-3000', '3000+'])
    price_dist = products.groupby('price_range').agg(
        product_count=('product_id', 'count'), total_sales=('sales_count', 'sum')
    ).reset_index()

    # Top cross-sell pairs (simple: products in same order)
    order_pairs = orders.groupby('order_id')['product_id'].apply(list).reset_index()
    pairs_count = {}
    for _, row in order_pairs.iterrows():
        prods = row['product_id']
        for i in range(len(prods)):
            for j in range(i+1, len(prods)):
                key = tuple(sorted([prods[i], prods[j]]))
                pairs_count[key] = pairs_count.get(key, 0) + 1

    top_pairs = sorted(pairs_count.items(), key=lambda x: x[1], reverse=True)[:10]
    # Get product names for top pairs
    prod_names = dict(zip(products['product_id'], products['product_name']))
    cross_sell = []
    for (p1, p2), cnt in top_pairs:
        cross_sell.append({
            'product_a': prod_names.get(p1, p1)[:15],
            'product_b': prod_names.get(p2, p2)[:15],
            'count': cnt
        })

    return {
        'category': category, 'brand_top10': brand_top10,
        'price_dist': price_dist, 'cross_sell': cross_sell
    }


# ============================================================
# SECTION 4: User Profile (Tab 4)
# ============================================================
def compute_user_profile(users, features):
    gender = users['gender'].value_counts().reset_index()
    gender.columns = ['gender', 'count']

    age_bins = [0, 18, 25, 30, 35, 40, 50, 100]
    age_labels = ['<18', '18-24', '25-29', '30-34', '35-39', '40-49', '50+']
    users['age_group'] = pd.cut(users['age'], bins=age_bins, labels=age_labels)
    age_dist = users['age_group'].value_counts().sort_index().reset_index()
    age_dist.columns = ['age_group', 'count']

    member = users['member_level'].value_counts().reset_index()
    member.columns = ['level', 'count']

    province = users['province'].value_counts().head(10).reset_index()
    province.columns = ['province', 'count']

    consumption = features['consumption_level'].value_counts().reset_index()
    consumption.columns = ['level', 'count']

    # Cross: member_level vs consumption
    user_merged = users.merge(features[['user_id', 'consumption_level']], on='user_id')
    cross = user_merged.groupby(['member_level', 'consumption_level']).size().unstack(fill_value=0)

    return {
        'gender': gender, 'age_dist': age_dist, 'member': member,
        'province': province, 'consumption': consumption, 'cross': cross
    }


# ============================================================
# SECTION 5: Behavior Metrics (Tab 5)
# ============================================================
def compute_behavior_metrics(behaviors, orders):
    total_behaviors = len(behaviors)

    behavior_counts = behaviors['behavior_type'].value_counts()

    # Funnel with conversion rates
    browse = behavior_counts.get('浏览', 1)
    click = behavior_counts.get('点击', 0)
    favorite = behavior_counts.get('收藏', 0)
    cart = behavior_counts.get('加购', 0)
    order_count = len(orders)

    funnel = pd.DataFrame({
        'stage': ['浏览', '点击', '收藏', '加购', '下单'],
        'count': [browse, click, favorite, cart, order_count],
        'rate': [
            '100%',
            f'{click/browse*100:.1f}%',
            f'{favorite/browse*100:.1f}%',
            f'{cart/browse*100:.1f}%',
            f'{order_count/browse*100:.1f}%'
        ]
    })

    # Daily behaviors vs revenue (already in daily from revenue section)
    daily_behaviors = behaviors.groupby('behavior_date').size().reset_index()
    daily_behaviors.columns = ['date', 'behavior_count']
    daily_behaviors['date'] = pd.to_datetime(daily_behaviors['date'])

    # Hourly heatmap data
    hourly = behaviors.groupby('behavior_hour').size().reset_index()
    hourly.columns = ['hour', 'count']

    # Duration distribution
    duration_bins = [0, 10, 30, 60, 120, 300, 600, 99999]
    duration_labels = ['0-10s', '10-30s', '30-60s', '1-2min', '2-5min', '5-10min', '10min+']
    behaviors['duration_range'] = pd.cut(behaviors['duration_seconds'], bins=duration_bins, labels=duration_labels)
    duration_dist = behaviors['duration_range'].value_counts().sort_index().reset_index()
    duration_dist.columns = ['range', 'count']

    # User activeness distribution (behaviors per user)
    user_behavior_count = behaviors.groupby('user_id').size()
    activeness_bins = [0, 5, 10, 20, 50, 100, 99999]
    activeness_labels = ['1-5', '6-10', '11-20', '21-50', '51-100', '100+']
    user_behavior_count_cut = pd.cut(user_behavior_count, bins=activeness_bins, labels=activeness_labels)
    activeness_dist = user_behavior_count_cut.value_counts().sort_index().reset_index()
    activeness_dist.columns = ['range', 'count']

    return {
        'funnel': funnel, 'daily_behaviors': daily_behaviors,
        'hourly': hourly, 'duration_dist': duration_dist,
        'activeness_dist': activeness_dist, 'total_behaviors': total_behaviors
    }


# ============================================================
# SECTION 6: User Clustering (Tab 6)
# ============================================================
def compute_clustering(users, orders, features, behaviors):
    """K-Means clustering with RFM features"""
    # RFM
    ref_date = orders['order_date_dt'].max() + pd.Timedelta(days=1)
    rfm = orders.groupby('user_id').agg(
        Recency=('order_date_dt', lambda x: (ref_date - x.max()).days),
        Frequency=('order_id', 'count'),
        Monetary=('actual_payment', 'sum')
    ).reset_index()

    # Behavior pivot
    behavior_pivot = behaviors.pivot_table(
        index='user_id', columns='behavior_type', values='behavior_id',
        aggfunc='count', fill_value=0
    ).reset_index()

    # Merge
    profile = users.merge(features, on='user_id', how='left')
    profile = profile.merge(rfm, on='user_id', how='left')
    profile = profile.merge(behavior_pivot, on='user_id', how='left')
    for col in ['Recency', 'Frequency', 'Monetary']:
        profile[col] = profile[col].fillna(0)
    for col in ['浏览', '点击', '收藏', '加购']:
        if col in profile.columns:
            profile[col] = profile[col].fillna(0).astype(int)

    # Cluster features
    cluster_cols = ['total_spent', 'order_count', 'avg_order_amount',
                    'browse_count', 'favorite_count', 'cart_count',
                    'Recency', 'Frequency', 'Monetary', 'purchase_intent', 'member_level_score']
    available_cols = [c for c in cluster_cols if c in profile.columns]
    cluster_data = profile[available_cols].fillna(0)

    scaler = StandardScaler()
    scaled_data = scaler.fit_transform(cluster_data)

    # K range evaluation - use sample for silhouette to avoid memory error
    import numpy as np
    np.random.seed(42)
    sample_idx = np.random.choice(len(scaled_data), min(1000, len(scaled_data)), replace=False)
    scaled_sample = scaled_data[sample_idx]

    k_range = range(2, 9)
    inertias, sil_scores = [], []
    for k in k_range:
        km = KMeans(n_clusters=k, random_state=42, n_init=10, max_iter=300)
        labels = km.fit_predict(scaled_data)
        inertias.append(km.inertia_)
        sil_scores.append(silhouette_score(scaled_sample, labels[sample_idx]))

    # Use K=5 for business relevance
    best_k = 5
    final_km = KMeans(n_clusters=best_k, random_state=42, n_init=10, max_iter=300)
    profile['cluster'] = final_km.fit_predict(scaled_data)
    sil = silhouette_score(scaled_sample, profile['cluster'].values[sample_idx])
    cal = calinski_harabasz_score(scaled_data, profile['cluster'])

    # PCA
    pca = PCA(n_components=2, random_state=42)
    pca_result = pca.fit_transform(scaled_data)
    profile['pca_x'] = pca_result[:, 0]
    profile['pca_y'] = pca_result[:, 1]
    pca_var = pca.explained_variance_ratio_ * 100

    # Cluster profiles
    cluster_profile = profile.groupby('cluster').agg(
        人数=('user_id', 'count'),
        平均年龄=('age', 'mean'),
        平均消费=('total_spent', 'mean'),
        订单数=('order_count', 'mean'),
        客单价=('avg_order_amount', 'mean'),
        浏览次数=('browse_count', 'mean'),
        收藏次数=('favorite_count', 'mean'),
        加购次数=('cart_count', 'mean'),
        R值=('Recency', 'mean'),
        F值=('Frequency', 'mean'),
        M值=('Monetary', 'mean'),
    ).round(1)

    # Unique naming by RFM rank
    r_rank = cluster_profile['R值'].rank(ascending=True)
    f_rank = cluster_profile['F值'].rank(ascending=False)
    m_rank = cluster_profile['M值'].rank(ascending=False)
    total_rank = r_rank + f_rank + m_rank
    sorted_clusters = total_rank.sort_values().index.tolist()
    name_labels = ['高价值忠诚用户', '高消费活跃用户', '高频活跃用户',
                   '沉睡高价值用户', '新晋潜力用户']
    cluster_names = {}
    for i, c in enumerate(sorted_clusters):
        cluster_names[c] = name_labels[i] if i < len(name_labels) else f'用户群体{i+1}'

    cluster_profile.insert(0, '群体标签', cluster_profile.index.map(cluster_names))
    profile['cluster_name'] = profile['cluster'].map(cluster_names)

    # Gender & consumption distribution by cluster
    gender_dist = profile.groupby(['cluster', 'gender']).size().unstack(fill_value=0)
    for g in gender_dist.columns:
        cluster_profile[f'{g}_占比'] = (gender_dist[g] / gender_dist.sum(axis=1) * 100).round(1)

    if 'consumption_level' in profile.columns:
        cons_dist = profile.groupby(['cluster', 'consumption_level']).size().unstack(fill_value=0)
        for l in cons_dist.columns:
            cluster_profile[f'消费{l}_占比'] = (cons_dist[l] / cons_dist.sum(axis=1) * 100).round(1)

    return {
        'profile': profile, 'cluster_profile': cluster_profile,
        'cluster_names': cluster_names, 'best_k': best_k,
        'k_range': list(k_range), 'inertias': inertias, 'sil_scores': sil_scores,
        'silhouette': sil, 'calinski': cal, 'pca_var': pca_var
    }


# ============================================================
# SECTION 7: Reviews & Service (Tab 7)
# ============================================================
def compute_review_metrics(orders):
    # Review scores
    review_dist = orders[orders['review_score'].notna()]['review_score'].value_counts().sort_index()
    avg_review = orders['review_score'].mean()

    # Monthly review trend
    monthly_review = orders[orders['review_score'].notna()].groupby('order_month')['review_score'].mean().reset_index()
    monthly_review.columns = ['month', 'avg_score']

    # Delivery time (ship to receive)
    orders['delivery_dt'] = pd.to_datetime(orders['delivery_date'])
    orders['receive_dt'] = pd.to_datetime(orders['receive_date'])
    orders['delivery_days'] = (orders['receive_dt'] - orders['delivery_dt']).dt.days
    delivery_valid = orders[(orders['delivery_days'] >= 0) & (orders['delivery_days'] <= 30)]
    avg_delivery = delivery_valid['delivery_days'].mean()

    delivery_bins = [0, 1, 2, 3, 5, 7, 14, 31]
    delivery_labels = ['当日达', '1-2天', '2-3天', '3-5天', '5-7天', '7-14天', '14天+']
    delivery_valid['delivery_range'] = pd.cut(delivery_valid['delivery_days'], bins=delivery_bins, labels=delivery_labels)
    delivery_dist = delivery_valid['delivery_range'].value_counts().sort_index().reset_index()
    delivery_dist.columns = ['range', 'count']

    # Cancel & refund analysis
    cancel_refund = orders[orders['order_status'].isin(['已取消', '已退款'])]
    cancel_by_payment = cancel_refund.groupby('payment_method').size().reset_index(name='count')

    return {
        'review_dist': review_dist, 'avg_review': avg_review,
        'monthly_review': monthly_review, 'avg_delivery': avg_delivery,
        'delivery_dist': delivery_dist, 'cancel_by_payment': cancel_by_payment
    }


# ============================================================
# SECTION 7b: Monthly Data for Time Slider
# ============================================================
def compute_monthly_data(orders, behaviors):
    """Pre-compute monthly aggregated data for JS time filtering"""
    months = sorted(orders['order_month'].unique())
    monthly = {'months': months}

    # Revenue metrics per month
    monthly['gmv'] = []
    monthly['revenue'] = []
    monthly['orders'] = []
    monthly['avg_order'] = []
    monthly['discount_rate'] = []
    monthly['arpu'] = []
    monthly['unique_customers'] = []

    for m in months:
        mo = orders[orders['order_month'] == m]
        monthly['gmv'].append(round(mo['total_amount'].sum() / 10000, 2))
        monthly['revenue'].append(round(mo['actual_payment'].sum() / 10000, 2))
        monthly['orders'].append(int(len(mo)))
        monthly['avg_order'].append(round(mo['actual_payment'].mean(), 1) if len(mo) > 0 else 0)
        disc_rate = (mo['discount'].sum() / mo['total_amount'].sum() * 100) if mo['total_amount'].sum() > 0 else 0
        monthly['discount_rate'].append(round(disc_rate, 1))
        monthly['arpu'].append(round(mo['actual_payment'].sum() / mo['user_id'].nunique(), 1) if mo['user_id'].nunique() > 0 else 0)
        monthly['unique_customers'].append(int(mo['user_id'].nunique()))

    # Payment distribution per month
    monthly['payment_methods'] = sorted(orders['payment_method'].dropna().unique())
    monthly['payment_counts'] = {}
    monthly['payment_amounts'] = {}
    for pm in monthly['payment_methods']:
        monthly['payment_counts'][pm] = []
        monthly['payment_amounts'][pm] = []
        for m in months:
            pmo = orders[(orders['order_month'] == m) & (orders['payment_method'] == pm)]
            monthly['payment_counts'][pm].append(int(len(pmo)))
            monthly['payment_amounts'][pm].append(round(pmo['actual_payment'].sum() / 10000, 2))

    # Order status distribution per month
    monthly['statuses'] = sorted(orders['order_status'].dropna().unique())
    monthly['status_counts'] = {}
    for st in monthly['statuses']:
        monthly['status_counts'][st] = []
        for m in months:
            monthly['status_counts'][st].append(int(len(orders[(orders['order_month'] == m) & (orders['order_status'] == st)])))

    # User lifecycle metrics per month
    monthly['dau_mean'] = []
    monthly['mau'] = []
    monthly['new_users'] = []
    monthly['lost_users'] = []
    monthly['total_behaviors'] = []
    monthly['stickiness'] = []

    for i, m in enumerate(months):
        mo = orders[orders['order_month'] == m]
        bm = behaviors[behaviors['behavior_date'].between(
            pd.Timestamp(m + '-01').date(), (pd.Timestamp(m + '-01') + pd.offsets.MonthEnd(1)).date()
        )]
        monthly['total_behaviors'].append(int(len(bm)))
        # DAU mean for this month
        if len(bm) > 0:
            dau_m = bm.groupby('behavior_date')['user_id'].nunique()
            monthly['dau_mean'].append(round(dau_m.mean(), 0) if len(dau_m) > 0 else 0)
            monthly['mau'].append(int(bm['user_id'].nunique()))
            monthly['stickiness'].append(round(dau_m.mean() / bm['user_id'].nunique() * 100, 1) if bm['user_id'].nunique() > 0 else 0)
        else:
            monthly['dau_mean'].append(0)
            monthly['mau'].append(0)
            monthly['stickiness'].append(0)

        # New users this month (first order ever)
        if i == 0:
            monthly['new_users'].append(int(mo['user_id'].nunique()))
        else:
            prev_users = set(orders[orders['order_month'].isin(months[:i])]['user_id'].unique())
            curr_users = set(mo['user_id'].unique())
            monthly['new_users'].append(int(len(curr_users - prev_users)))

        # Lost users this month (>30 days no activity)
        if len(bm) > 0 and i < len(months) - 1:
            active_this_month = set(bm['user_id'].unique())
            next_bm = behaviors[behaviors['behavior_date'].between(
                pd.Timestamp(months[i+1] + '-01').date(), (pd.Timestamp(months[i+1] + '-01') + pd.offsets.MonthEnd(1)).date()
            )]
            if len(next_bm) > 0:
                active_next_month = set(next_bm['user_id'].unique())
                monthly['lost_users'].append(int(len(active_this_month - active_next_month)))
            else:
                monthly['lost_users'].append(0)
        else:
            monthly['lost_users'].append(0)

    # Category sales per month - requires merged order_product data; skipped for JS time filter
    monthly['top_categories'] = []

    # Behavior type distribution per month
    monthly['behavior_types'] = sorted(behaviors['behavior_type'].dropna().unique())
    monthly['behavior_counts'] = {}
    for bt in monthly['behavior_types']:
        monthly['behavior_counts'][bt] = []
        for m in months:
            btm = behaviors[behaviors['behavior_date'].between(
                pd.Timestamp(m + '-01').date(), (pd.Timestamp(m + '-01') + pd.offsets.MonthEnd(1)).date()
            )]
            monthly['behavior_counts'][bt].append(int(len(btm[btm['behavior_type'] == bt])))

    # Hourly behavior distribution per month
    monthly['hourly_counts'] = []
    for m in months:
        bhm = behaviors[behaviors['behavior_date'].between(
            pd.Timestamp(m + '-01').date(), (pd.Timestamp(m + '-01') + pd.offsets.MonthEnd(1)).date()
        )]
        if len(bhm) > 0:
            hourly = bhm.groupby('behavior_hour').size()
            monthly['hourly_counts'].append({int(h): int(c) for h, c in hourly.items()})
        else:
            monthly['hourly_counts'].append({})

    # Review score per month
    monthly['review_scores'] = []
    for m in months:
        rmo = orders[(orders['order_month'] == m) & (orders['review_score'].notna())]
        monthly['review_scores'].append(round(rmo['review_score'].mean(), 2) if len(rmo) > 0 else 0)

    # Month date ranges (for daily chart x-axis filtering)
    monthly['month_dates'] = {}
    for m in months:
        m_start = pd.Timestamp(m + '-01').date()
        m_end = (pd.Timestamp(m + '-01') + pd.offsets.MonthEnd(1)).date()
        monthly['month_dates'][m] = {'start': str(m_start), 'end': str(m_end)}

    # Lifecycle stages per month (active/silent/lost for stacked bars)
    monthly['lifecycle_active'] = []
    monthly['lifecycle_silent'] = []
    monthly['lifecycle_lost'] = []
    monthly['lifecycle_new'] = []
    for i, m in enumerate(months):
        mo = orders[orders['order_month'] == m]
        bm = behaviors[behaviors['behavior_date'].between(
            pd.Timestamp(m + '-01').date(), (pd.Timestamp(m + '-01') + pd.offsets.MonthEnd(1)).date()
        )]
        users_with_order = set(mo['user_id'].unique())
        users_with_behavior = set(bm['user_id'].unique()) if len(bm) > 0 else set()
        # Active: had order or behavior this month
        active = users_with_order | users_with_behavior
        # New: first order this month (simplified: not in previous months)
        if i == 0:
            new = len(users_with_order)
        else:
            prev_months = months[:i]
            prev_orders = orders[orders['order_month'].isin(prev_months)]
            prev_users = set(prev_orders['user_id'].unique())
            new = len(users_with_order - prev_users)
        # Silent: no order or behavior this month but had activity in previous months
        if i > 0:
            all_prev_active = set()
            for pm in prev_months:
                pmo = orders[orders['order_month'] == pm]
                pbm = behaviors[behaviors['behavior_date'].between(
                    pd.Timestamp(pm + '-01').date(), (pd.Timestamp(pm + '-01') + pd.offsets.MonthEnd(1)).date()
                )]
                all_prev_active |= set(pmo['user_id'].unique())
                if len(pbm) > 0:
                    all_prev_active |= set(pbm['user_id'].unique())
            silent = len(all_prev_active - active)
        else:
            silent = 0
        # Total users ever active up to this month
        monthly['lifecycle_active'].append(len(active))
        monthly['lifecycle_silent'].append(silent)
        monthly['lifecycle_lost'].append(0)  # simplified: lost = total_ever - active - silent
        monthly['lifecycle_new'].append(new)

    # Category sales per month (for product tab)
    monthly['category_monthly'] = {}
    from itertools import product as _product
    categories = orders['category'].dropna().unique() if 'category' in orders.columns else []
    for cat in categories:
        monthly['category_monthly'][cat] = []
        for m in months:
            cmo = orders[(orders['order_month'] == m) & (orders['category'] == cat)]
            monthly['category_monthly'][cat].append(round(cmo['actual_payment'].sum() / 10000, 2))

    # Gender/Age/Member monthly breakdown (for user profile tab KPIs)
    if 'gender' in orders.columns:
        monthly['gender_monthly'] = {}
        for g in orders['gender'].dropna().unique():
            monthly['gender_monthly'][g] = []
            for m in months:
                monthly['gender_monthly'][g].append(int(len(orders[(orders['order_month'] == m) & (orders['gender'] == g)]['user_id'].unique())))

    if 'age_group' in orders.columns:
        monthly['age_monthly'] = {}
        for a in orders['age_group'].dropna().unique():
            monthly['age_monthly'][a] = []
            for m in months:
                monthly['age_monthly'][a].append(int(len(orders[(orders['order_month'] == m) & (orders['age_group'] == a)]['user_id'].unique())))

    return monthly


# ============================================================
# SECTION 7c: Sankey Data - User Segment Migration
# ============================================================
def compute_sankey_data(users, orders, behaviors):
    """Compute user segment migration between two periods for Sankey diagram"""
    from datetime import datetime as dt

    # Split data into two equal periods
    all_dates = orders['order_date_dt'].dropna()
    mid_date = all_dates.min() + (all_dates.max() - all_dates.min()) / 2
    p1_start = all_dates.min()
    p1_end = mid_date
    p2_start = mid_date + pd.Timedelta(days=1)
    p2_end = all_dates.max()

    periods = {
        'P1': {'start': p1_start, 'end': p1_end, 'label': f'{p1_start.strftime("%Y-%m-%d")} ~ {p1_end.strftime("%Y-%m-%d")}'},
        'P2': {'start': p2_start, 'end': p2_end, 'label': f'{p2_start.strftime("%Y-%m-%d")} ~ {p2_end.strftime("%Y-%m-%d")}'}
    }

    def classify_lifecycle(user_id, period_start, period_end):
        """Classify user lifecycle stage in a given period"""
        user_orders = orders[(orders['user_id'] == user_id) &
                             (orders['order_date_dt'] >= period_start) &
                             (orders['order_date_dt'] <= period_end)]
        user_behaviors = behaviors[(behaviors['user_id'] == user_id) &
                                    (behaviors['behavior_time_dt'] >= period_start) &
                                    (behaviors['behavior_time_dt'] <= period_end)]

        has_order = len(user_orders) > 0
        has_behavior = len(user_behaviors) > 0

        if has_order:
            first_order = user_orders['order_date_dt'].min()
            if first_order >= period_start:
                return '新用户'
            elif has_behavior and user_behaviors['behavior_time_dt'].max() >= period_end - pd.Timedelta(days=30):
                return '活跃用户'
            else:
                return '沉默用户'
        elif has_behavior:
            last_behavior = user_behaviors['behavior_time_dt'].max()
            if last_behavior >= period_end - pd.Timedelta(days=30):
                return '活跃用户'
            else:
                return '沉默用户'
        else:
            return '流失用户'

    def classify_rfm(user_id, period_start, period_end):
        """Classify user into RFM value tier in a given period"""
        user_orders = orders[(orders['user_id'] == user_id) &
                             (orders['order_date_dt'] >= period_start) &
                             (orders['order_date_dt'] <= period_end)]
        if len(user_orders) == 0:
            return '无消费'
        total = user_orders['actual_payment'].sum()
        if total >= 500:
            return '高价值'
        elif total >= 150:
            return '中价值'
        else:
            return '低价值'

    # Get all users who had activity in either period
    p1_users = set(orders[(orders['order_date_dt'] >= p1_start) & (orders['order_date_dt'] <= p1_end)]['user_id'].unique())
    p2_users = set(orders[(orders['order_date_dt'] >= p2_start) & (orders['order_date_dt'] <= p2_end)]['user_id'].unique())
    all_active = p1_users | p2_users

    # Classify each user in both periods
    p1_lifecycle = {}
    p1_rfm = {}
    p2_lifecycle = {}
    p2_rfm = {}
    for uid in all_active:
        p1_lifecycle[uid] = classify_lifecycle(uid, p1_start, p1_end)
        p2_lifecycle[uid] = classify_lifecycle(uid, p2_start, p2_end)
        p1_rfm[uid] = classify_rfm(uid, p1_start, p1_end)
        p2_rfm[uid] = classify_rfm(uid, p2_start, p2_end)

    # Build migration matrix for lifecycle
    lc_stages = ['新用户', '活跃用户', '沉默用户', '流失用户']
    lc_matrix = {}
    for s1 in lc_stages:
        lc_matrix[s1] = {}
        for s2 in lc_stages:
            lc_matrix[s1][s2] = 0

    for uid in all_active:
        s1 = p1_lifecycle.get(uid, '流失用户')
        s2 = p2_lifecycle.get(uid, '流失用户')
        lc_matrix[s1][s2] += 1

    # Build Sankey for lifecycle
    lc_source, lc_target, lc_value = [], [], []
    lc_labels = [f'P1-{s}' for s in lc_stages] + [f'P2-{s}' for s in lc_stages]
    for i, s1 in enumerate(lc_stages):
        for j, s2 in enumerate(lc_stages):
            if lc_matrix[s1][s2] > 0:
                lc_source.append(i)
                lc_target.append(len(lc_stages) + j)
                lc_value.append(lc_matrix[s1][s2])

    # Build migration matrix for RFM
    rfm_stages = ['高价值', '中价值', '低价值', '无消费']
    rfm_matrix = {}
    for s1 in rfm_stages:
        rfm_matrix[s1] = {}
        for s2 in rfm_stages:
            rfm_matrix[s1][s2] = 0

    for uid in all_active:
        s1 = p1_rfm.get(uid, '无消费')
        s2 = p2_rfm.get(uid, '无消费')
        rfm_matrix[s1][s2] += 1

    # Build Sankey for RFM
    rfm_source, rfm_target, rfm_value = [], [], []
    rfm_labels = [f'P1-{s}' for s in rfm_stages] + [f'P2-{s}' for s in rfm_stages]
    for i, s1 in enumerate(rfm_stages):
        for j, s2 in enumerate(rfm_stages):
            if rfm_matrix[s1][s2] > 0:
                rfm_source.append(i)
                rfm_target.append(len(rfm_stages) + j)
                rfm_value.append(rfm_matrix[s1][s2])

    # Net flow analysis
    lc_net = {}
    for s in lc_stages:
        inflow = sum(lc_matrix[os][s] for os in lc_stages if os != s)
        outflow = sum(lc_matrix[s][os] for os in lc_stages if os != s)
        lc_net[s] = inflow - outflow

    # Retention rates
    lc_retention = {}
    for s in lc_stages:
        total = sum(lc_matrix[s].values())
        lc_retention[s] = round(lc_matrix[s][s] / total * 100, 1) if total > 0 else 0

    return {
        'periods': periods,
        'lc_stages': lc_stages,
        'lc_matrix': lc_matrix,
        'lc_source': lc_source, 'lc_target': lc_target, 'lc_value': lc_value, 'lc_labels': lc_labels,
        'rfm_stages': rfm_stages,
        'rfm_matrix': rfm_matrix,
        'rfm_source': rfm_source, 'rfm_target': rfm_target, 'rfm_value': rfm_value, 'rfm_labels': rfm_labels,
        'lc_net': lc_net,
        'lc_retention': lc_retention,
        'total_users': len(all_active)
    }


# ============================================================
# SECTION 8: HTML Generation - Tabbed Layout
# ============================================================
def generate_unified_html(all_data):
    """Generate complete tabbed HTML report"""
    rev = all_data['revenue']
    lc = all_data['lifecycle']
    prod = all_data['product']
    up = all_data['user_profile']
    beh = all_data['behavior']
    cl = all_data['clustering']
    rv = all_data['review']
    monthly = all_data.get('monthly', {})
    sankey = all_data.get('sankey', {})

    # Color palette
    C = {
        'primary': '#667eea', 'secondary': '#764ba2', 'accent': '#e74c3c',
        'green': '#27ae60', 'orange': '#e67e22', 'teal': '#1abc9c',
        'blue': '#2980b9', 'purple': '#8e44ad', 'gray': '#95a5a6',
        'cluster': ['#667eea', '#e74c3c', '#27ae60', '#f39c12', '#8e44ad']
    }

    tabs = ['分析首页', '营收总览', '用户生命周期', '产品分析', '用户画像', '行为分析', '用户聚类', '分层迁移', '评价服务']
    tab_ids = ['tab0', 'tab1', 'tab2', 'tab3', 'tab4', 'tab5', 'tab6', 'tab7', 'tab8']

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>天猫用户销售数据 - 统一分析报表</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; background: #f0f2f5; color: #333; }}
.header {{ background: linear-gradient(135deg, {C['primary']} 0%, {C['secondary']} 100%); color: white; padding: 24px 40px; }}
.header h1 {{ font-size: 26px; margin-bottom: 6px; }}
.header p {{ opacity: 0.85; font-size: 13px; }}
.container {{ max-width: 1440px; margin: 0 auto; padding: 16px; }}

/* Tabs */
.tab-wrap {{ display: flex; flex-wrap: wrap; gap: 0; margin-bottom: 0; }}
.tab-wrap input[type="radio"] {{ display: none; }}
.tab-label {{
    padding: 12px 20px; background: #fff; border-bottom: 3px solid transparent;
    cursor: pointer; font-size: 14px; font-weight: 500; color: #888;
    transition: all 0.2s; border-radius: 8px 8px 0 0; margin-right: 2px;
}}
.tab-label:hover {{ color: {C['primary']}; background: #f8f9ff; }}
.tab-wrap input[type="radio"]:checked + .tab-label {{
    color: {C['primary']}; border-bottom-color: {C['primary']}; background: #fff;
    font-weight: 600;
}}
.tab-content {{ display: none; background: #fff; border-radius: 0 0 10px 10px; padding: 20px; }}
#tab0-c:checked ~ #content0,
#tab1-c:checked ~ #content1,
#tab2-c:checked ~ #content2,
#tab3-c:checked ~ #content3,
#tab4-c:checked ~ #content4,
#tab5-c:checked ~ #content5,
#tab6-c:checked ~ #content6,
#tab7-c:checked ~ #content7,
#tab8-c:checked ~ #content8 {{ display: block; }}

/* KPI */
.kpi-row {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 14px; margin-bottom: 20px; }}
.kpi-row.four {{ grid-template-columns: repeat(4, 1fr); }}
.kpi-card {{ background: linear-gradient(135deg, #f8f9ff, #fff); border-radius: 10px; padding: 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; border: 1px solid #f0f0f0; }}
.kpi-card .label {{ font-size: 12px; color: #999; margin-bottom: 4px; }}
.kpi-card .value {{ font-size: 24px; font-weight: bold; color: #333; }}
.kpi-card .sub {{ font-size: 11px; color: #aaa; margin-top: 2px; }}
.color-primary {{ color: {C['primary']}; }}
.color-green {{ color: {C['green']}; }}
.color-orange {{ color: {C['orange']}; }}
.color-accent {{ color: {C['accent']}; }}
.color-teal {{ color: {C['teal']}; }}
.color-purple {{ color: {C['purple']}; }}

/* Chart grid */
.chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }}
.chart-row.full {{ grid-template-columns: 1fr; }}
.chart-row.three {{ grid-template-columns: 1fr 1fr 1fr; }}
@media (max-width: 1100px) {{ .chart-row.three {{ grid-template-columns: 1fr 1fr; }} }}
@media (max-width: 768px) {{ .chart-row, .chart-row.three {{ grid-template-columns: 1fr; }} }}
.chart-card {{ background: #fafbff; border-radius: 10px; padding: 18px; border: 1px solid #f0f0ff; min-width: 0; overflow: hidden; }}
.chart-card h3 {{ font-size: 15px; color: #555; margin-bottom: 10px; padding-bottom: 8px; border-bottom: 2px solid #f0f2f5; }}
.section-title {{ font-size: 20px; color: #333; margin: 0 0 16px 0; padding-left: 12px; border-left: 4px solid {C['primary']}; }}
.insight-box {{ background: #f8f9ff; border-left: 3px solid {C['primary']}; padding: 12px 16px; margin: 12px 0; border-radius: 0 8px 8px 0; font-size: 13px; color: #666; }}
.insight-box-top {{ background: linear-gradient(135deg, #f8f9ff, #eef0ff); border-left: 4px solid {C['primary']}; padding: 14px 18px; margin: 0 0 20px 0; border-radius: 0 10px 10px 0; font-size: 13px; color: #444; }}

/* Tables */
.table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; max-width: 100%; }}
.data-table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; table-layout: auto; word-break: break-all; }}
.data-table th {{ background: {C['primary']}; color: white; padding: 8px 6px; white-space: nowrap; }}
.data-table td {{ padding: 6px; text-align: center; border-bottom: 1px solid #eee; max-width: 200px; overflow: hidden; text-overflow: ellipsis; }}
.data-table tr:hover {{ background: #f8f9ff; }}

/* Homepage */
.home-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
@media (max-width: 768px) {{ .home-grid {{ grid-template-columns: 1fr; }} }}
.home-card {{ background: linear-gradient(135deg, #fafbff, #fff); border-radius: 12px; padding: 20px; border: 1px solid #eef0ff; }}
.home-card h3 {{ font-size: 16px; margin-bottom: 12px; }}
.home-card h3 .num {{ display: inline-block; width: 28px; height: 28px; line-height: 28px; text-align: center; background: {C['primary']}; color: #fff; border-radius: 50%; margin-right: 8px; font-size: 13px; }}
.finding-item {{ padding: 8px 0; border-bottom: 1px solid #f0f0f0; font-size: 13px; color: #555; }}
.finding-item:last-child {{ border-bottom: none; }}
.finding-item .highlight {{ color: {C['accent']}; font-weight: bold; }}
.tag {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 11px; color: white; }}

.hidden-init {{ visibility: hidden; height: 0; overflow: hidden; }}

.footer {{ text-align: center; padding: 20px; color: #999; font-size: 12px; margin-top: 20px; }}

/* Time Filter */
/* Time Range Slider */
.time-filter {{ background: #fff; border-bottom: 1px solid #e8e8e8; padding: 14px 40px; display: flex; flex-wrap: wrap; gap: 14px; align-items: center; font-size: 13px; }}
.time-filter .filter-label {{ font-weight: 600; color: #555; white-space: nowrap; }}
.time-filter .slider-wrapper {{ flex: 1; min-width: 280px; position: relative; padding: 10px 0 4px; }}
.time-filter .slider-track {{ position: relative; height: 6px; background: #e0e0e0; border-radius: 3px; }}
.time-filter .slider-fill {{ position: absolute; height: 6px; background: linear-gradient(90deg, {C['primary']}, {C['secondary']}); border-radius: 3px; transition: left 0.1s, right 0.1s; }}
.time-filter input[type="range"] {{
    position: absolute; top: -8px; left: 0; width: 100%; height: 6px;
    background: transparent; -webkit-appearance: none; pointer-events: none;
    margin: 0; padding: 0; z-index: 2;
}}
.time-filter input[type="range"]::-webkit-slider-thumb {{
    -webkit-appearance: none; pointer-events: all; width: 28px; height: 28px;
    background: #fff; border: 3px solid {C['primary']}; border-radius: 50%;
    cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.15); transition: transform 0.15s;
}}
.time-filter input[type="range"]::-webkit-slider-thumb:hover {{ transform: scale(1.2); }}
.time-filter input[type="range"]::-moz-range-thumb {{
    pointer-events: all; width: 28px; height: 28px; background: #fff;
    border: 3px solid {C['primary']}; border-radius: 50%; cursor: pointer;
}}
.time-filter .slider-labels {{ display: flex; justify-content: space-between; margin-top: 6px; font-size: 11px; color: #999; }}
.time-filter .slider-value {{ background: {C['primary']}; color: #fff; padding: 4px 12px; border-radius: 4px; font-size: 12px; font-weight: 600; min-width: 90px; text-align: center; }}
.time-filter .btn-reset {{ padding: 6px 12px; background: #fff; color: #888; border: 1px solid #ddd; border-radius: 6px; cursor: pointer; font-size: 13px; white-space: nowrap; }}
.time-filter .btn-reset:hover {{ background: #f5f5f5; }}
.time-filter .filter-info {{ color: #aaa; font-size: 12px; white-space: nowrap; }}

.meta-bar {{ background: #fff; border-bottom: 1px solid #e8e8e8; padding: 12px 40px; display: flex; flex-wrap: wrap; gap: 20px; align-items: center; font-size: 12px; color: #888; }}
.meta-bar .meta-item {{ display: flex; align-items: center; gap: 6px; }}
.meta-bar .meta-label {{ font-weight: 600; color: #555; }}
.meta-bar .meta-value {{ color: #333; }}
.meta-bar .meta-tag {{ display: inline-block; padding: 2px 8px; background: #f0f2ff; color: {C['primary']}; border-radius: 4px; font-size: 11px; }}
</style>
</head>
<body>

<div class="header">
    <h1>天猫用户销售数据 统一分析报表</h1>
    <p>数据范围: 2025-09-06 至 2026-03-05 | 用户数: {rev['unique_customers']:,} | 订单数: {rev['total_orders']:,} |
    产品数: {len(all_data.get('products', pd.DataFrame())):,} | 行为数据: {beh['total_behaviors']:,}</p>
</div>

<div class="meta-bar">
    <div class="meta-item"><span class="meta-label">作者:</span><span class="meta-value">kadima</span></div>
    <div class="meta-item"><span class="meta-label">完成时间:</span><span class="meta-value">{datetime.now().strftime('%Y-%m-%d %H:%M')}</span></div>
    <div class="meta-item"><span class="meta-label">AI配置:</span><span class="meta-tag">Claude Code</span><span class="meta-tag">DeepSeek v4 Pro</span></div>
    <div class="meta-item"><span class="meta-label">技术栈:</span><span class="meta-tag">SQL (MySQL 8.0)</span><span class="meta-tag">Python 3.11+</span><span class="meta-tag">pandas</span><span class="meta-tag">sklearn</span><span class="meta-tag">Plotly</span><span class="meta-tag">python-docx</span></div>
</div>

<div class="time-filter">
    <span class="filter-label">{chr(128466)} 日期范围筛选:</span>
    <span class="slider-value" id="slider-start-disp">{monthly.get("months", [""])[0]}</span>
    <div class="slider-wrapper" id="slider-wrapper">
        <div class="slider-track">
            <div class="slider-fill" id="slider-fill"></div>
        </div>
        <input type="range" id="range-start" min="0" max="{len(monthly.get('months', []))-1}" value="0" step="1" oninput="onSliderChange()">
        <input type="range" id="range-end" min="0" max="{len(monthly.get('months', []))-1}" value="{len(monthly.get('months', []))-1}" step="1" oninput="onSliderChange()">
        <div class="slider-labels" id="slider-labels"></div>
    </div>
    <span class="slider-value" id="slider-end-disp">{monthly.get("months", [""])[-1]}</span>
    <button class="btn-reset" onclick="resetTimeFilter()" title="恢复全部时段">&#x21BA; 重置</button>
    <span class="filter-info" id="filter-info">当前: 全部时段 ({len(monthly.get('months', []))}个月)</span>
</div>

<div class="container">

<div class="tab-wrap">
'''

    # Tab radio buttons
    for i, (tid, tname) in enumerate(zip(tab_ids, tabs)):
        checked = 'checked' if i == 0 else ''
        html += f'    <input type="radio" name="tab" id="{tid}-c" {checked}><label class="tab-label" for="{tid}-c">{tname}</label>\n'

    html += '\n'

    # ================================================================
    # TAB 1: Revenue Overview
    # ================================================================
    mom_arrow = '↑' if rev['last_mom'] > 0 else '↓'
    mom_color = 'color-green' if rev['last_mom'] > 0 else 'color-accent'

    # ================================================================
    # TAB 0: 分析首页
    # ================================================================
    # Compute key stats for homepage
    lifecycle_dist = lc['lifecycle_dist']
    dormant_churn_pct = (lifecycle_dist.get('沉默用户', 0) + lifecycle_dist.get('流失用户', 0)) / rev['unique_customers'] * 100
    top_cat_name = prod['category']['category'].iloc[0]
    top_cat_pct = prod['category']['sales'].iloc[0] / prod['category']['sales'].sum() * 100
    conversion = rev['total_orders'] / max(beh['funnel']['count'].iloc[0], 1) * 100

    html += f'''<div id="content0" class="tab-content">

<div style="text-align:center;margin-bottom:24px;">
    <h2 style="font-size:22px;color:#333;">天猫用户销售数据 BI 分析报表</h2>
    <p style="color:#999;font-size:13px;">数据范围: 2025-09-06 至 2026-03-05 | 用户 {rev["unique_customers"]:,} 人 | 订单 {rev["total_orders"]:,} 单 | 产品 {len(all_data.get("products", [])):,} 个 | 行为 {beh["total_behaviors"]:,} 条</p>
</div>

<div class="home-grid">
    <div class="home-card">
        <h3><span class="num">1</span>营收总览</h3>
        <p style="color:#888;font-size:12px;">GMV、实收、客单价、折扣率、订单趋势、支付方式</p>
    </div>
    <div class="home-card">
        <h3><span class="num">2</span>用户生命周期</h3>
        <p style="color:#888;font-size:12px;">DAU/MAU、留存队列、流失分析、LTV、复购间隔</p>
    </div>
    <div class="home-card">
        <h3><span class="num">3</span>产品分析</h3>
        <p style="color:#888;font-size:12px;">品类排名、品牌Top10、价格分布、关联购买</p>
    </div>
    <div class="home-card">
        <h3><span class="num">4</span>用户画像</h3>
        <p style="color:#888;font-size:12px;">性别/年龄/会员/地域/消费能力五维度 + 交叉分析</p>
    </div>
    <div class="home-card">
        <h3><span class="num">5</span>行为分析</h3>
        <p style="color:#888;font-size:12px;">转化漏斗、时段分布、活跃度分层、时长分析</p>
    </div>
    <div class="home-card">
        <h3><span class="num">6</span>用户聚类</h3>
        <p style="color:#888;font-size:12px;">K-Means(K=5)用户分群、RFM雷达图、PCA可视化</p>
    </div>
    <div class="home-card">
        <h3><span class="num">7</span>评价服务</h3>
        <p style="color:#888;font-size:12px;">评分分布、月度趋势、物流时效、取消退款分析</p>
    </div>
</div>

<h3 style="font-size:18px;color:#333;margin:24px 0 14px 0;padding-left:12px;border-left:4px solid {C['accent']};">核心分析结论</h3>

<div class="finding-item"><span class="highlight">1. 营收健康</span> — 6个月GMV {rev["total_gmv"]:,.0f}万元，实收 {rev["total_revenue"]:,.0f}万元，折扣率 {rev["discount_rate"]:.1f}%，处行业正常区间。月度营收存在波动，最新月环比{abs(rev["last_mom"]):.1f}%。</div>
<div class="finding-item"><span class="highlight">2. 用户流失风险</span> — 沉默+流失用户占消费用户的 {dormant_churn_pct:.1f}%，远超健康线(40%)，用户资产保全为首要任务。</div>
<div class="finding-item"><span class="highlight">3. 转化效率有空间</span> — 浏览→下单转化率 {conversion:.2f}%，加购→下单转化率高，加购用户是最高效的转化节点。</div>
<div class="finding-item"><span class="highlight">4. 品类集中度高</span> — Top品类"{top_cat_name}"贡献 {top_cat_pct:.1f}% 销售，需平衡头部投入与长尾培育。</div>
<div class="finding-item"><span class="highlight">5. 用户聚类分层清晰</span> — K-Means(K=5)将用户分为5个差异化群体，其中"{cl['cluster_names'][0]}"({int(cl['cluster_profile'].iloc[0]['人数'])}人)和"{cl['cluster_names'][1]}"({int(cl['cluster_profile'].iloc[1]['人数'])}人)为两大核心群体，需制定差异化运营策略。</div>

<div style="margin-top:20px;padding:16px;background:#fff9f0;border-radius:10px;border:1px solid #f0e0c0;">
    <strong style="color:{C['orange']};">建议关注优先级:</strong>
    <span style="color:#666;font-size:13px;">P0 降低用户流失 → P0 提升转化率 → P1 优化品类结构 → P1 激活低消费用户 → P2 提升粘性</span>
</div>

<h3 style="font-size:18px;color:#333;margin:24px 0 14px 0;padding-left:12px;border-left:4px solid {C['green']};">核心指标字典</h3>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px;">
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['primary']};">GMV（商品交易总额）</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: SUM(订单标价 × 数量) | 衡量平台交易规模，未扣除折扣退款</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['green']};">实际营收</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: SUM(actual_payment) | 扣除折扣后实际到账金额，真实收入口径</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['orange']};">客单价（AOV）</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: 实际营收 ÷ 订单总数 | 反映用户单次消费水平，驱动连带率提升</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['accent']};">折扣率</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: (GMV - 实收) ÷ GMV × 100% | 行业正常区间12-18%，反映让利幅度</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['teal']};">复购率</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: (订单总数 - 消费用户数) ÷ 消费用户数 × 100% | 反映用户重复购买程度</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['purple']};">DAU / MAU（活跃度）</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">DAU=日去重行为用户 | MAU=月去重行为用户 | 粘性比=DAU÷MAU，>20%为健康</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['blue']};">LTV（用户生命周期价值）</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: SUM(用户所有订单actual_payment) | 衡量单用户长期价值，指导获客成本</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:#e74c3c;">留存率（Retention）</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: 第N周仍有行为的用户数 ÷ 首周队列总用户数 × 100% | 衡量用户粘性</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['orange']};">ARPU（每用户平均收入）</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: 月度营收 ÷ 月度消费用户数 | 衡量用户月度价值贡献</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['primary']};">转化率（CVR）</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: 下单数 ÷ 浏览数 × 100% | 反映流量到成交的转化效率</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['green']};">R/F/M值</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">R=最近购买距今天数 | F=累计购买次数 | M=累计消费金额 | RFM三维评估用户价值</p>
    </div>
    <div style="background:#fafbff;border-radius:8px;padding:12px;border:1px solid #eef0ff;">
        <strong style="color:{C['purple']};">轮廓系数（Silhouette）</strong>
        <p style="margin:4px 0 0;font-size:11px;color:#888;">公式: (b-a)/max(a,b) | 聚类质量评估指标，-1~1，越接近1聚类效果越好</p>
    </div>
</div>

</div>
'''

    html += f'''<div id="content1" class="tab-content">

<div class="kpi-row">
    <div class="kpi-card"><div class="label">总GMV（万元）</div><div class="value color-primary" id="kpi-gmv">{rev['total_gmv']:,.0f}</div><div class="sub">环比 {mom_arrow}{abs(rev['last_mom']):.1f}%</div></div>
    <div class="kpi-card"><div class="label">实际营收（万元）</div><div class="value color-green" id="kpi-revenue">{rev['total_revenue']:,.0f}</div></div>
    <div class="kpi-card"><div class="label">订单总数</div><div class="value color-purple" id="kpi-orders">{rev['total_orders']:,}</div></div>
    <div class="kpi-card"><div class="label">客单价（元）</div><div class="value color-orange" id="kpi-aov">{rev['avg_order_value']:,.0f}</div></div>
    <div class="kpi-card"><div class="label">折扣率</div><div class="value color-accent" id="kpi-discount">{rev['discount_rate']:.1f}%</div></div>
    <div class="kpi-card"><div class="label">复购率</div><div class="value color-teal" id="kpi-repurchase">{rev['repurchase']:.1f}%</div><div class="sub">均单数/消费用户</div></div>
</div>

<div class="insight-box">核心发现: 报告期总GMV {rev['total_gmv']:,.0f}万元，实际营收{rev['total_revenue']:,.0f}万元，整体折扣率{rev['discount_rate']:.1f}%处于电商行业正常区间。月度营收呈波动态势，建议关注季节性促销节点平抑收入波动。</div>

<div class="chart-row full"><div class="chart-card"><h3>月度营收 & GMV 趋势（含订单数）</h3><div class="plot" id="c1_revenue_monthly"></div></div></div>
<div class="chart-row full"><div class="chart-card"><h3>日度营收趋势（7日移动平均）</h3><div class="plot" id="c1_revenue_daily"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>月度订单量与客单价</h3><div class="plot" id="c1_orders_avg"></div></div>
<div class="chart-card"><h3>月度折扣率 & ARPU趋势</h3><div class="plot" id="c1_discount_arpu"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>支付方式分布</h3><div class="plot" id="c1_payment"></div></div>
<div class="chart-card"><h3>订单状态分布</h3><div class="plot" id="c1_status"></div></div></div>
</div>
'''

    # ================================================================
    # TAB 2: User Lifecycle
    # ================================================================
    lifecycle_dist = lc['lifecycle_dist']
    html += f'''<div id="content2" class="tab-content">

<div class="kpi-row four">
    <div class="kpi-card"><div class="label">日均活跃用户(DAU)</div><div class="value color-primary" id="kpi-dau">{lc['daily_active']['dau'].mean():.0f}</div></div>
    <div class="kpi-card"><div class="label">用户粘性(DAU/MAU)</div><div class="value color-green" id="kpi-stickiness">{lc['stickiness']:.1f}%</div></div>
    <div class="kpi-card"><div class="label">平均复购间隔(天)</div><div class="value color-orange" id="kpi-repurchase-interval">{lc['avg_repurchase_interval']:.1f}</div></div>
    <div class="kpi-card"><div class="label">用户平均LTV(元)</div><div class="value color-purple" id="kpi-ltv">{lc['avg_ltv']:,.0f}</div></div>
</div>

<div class="insight-box">用户生命周期分析: DAU/MAU粘性比{lc['stickiness']:.1f}%，反映用户回访频次。活跃用户{lifecycle_dist.get('活跃用户',0):,}人、沉默用户{lifecycle_dist.get('沉默用户',0):,}人、流失用户{lifecycle_dist.get('流失用户',0):,}人。沉默+流失用户合计占比{(lifecycle_dist.get('沉默用户',0)+lifecycle_dist.get('流失用户',0))/rev['unique_customers']*100:.1f}%，需重点关注用户唤醒策略。</div>

<div class="chart-row full"><div class="chart-card"><h3>DAU / WAU / MAU 活动用户趋势</h3><div class="plot" id="c2_dau_wau_mau"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>月度新增用户 vs 流失用户</h3><div class="plot" id="c2_new_lost"></div></div>
<div class="chart-card"><h3>用户生命周期阶段分布</h3><div class="plot" id="c2_lifecycle"></div></div></div>
<div class="chart-row full"><div class="chart-card"><h3>用户周度留存队列 (Cohort Retention)</h3><div class="plot" id="c2_retention"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>复购间隔分布</h3><div class="plot" id="c2_repurchase_interval"></div></div>
<div class="chart-card"><h3>LTV 按订单数分布</h3><div class="plot" id="c2_ltv"></div></div></div>
</div>
'''

    # ================================================================
    # TAB 3: Product Analysis
    # ================================================================
    cross_html = '<div class="table-wrap"><table class="data-table"><tr><th>商品A</th><th>商品B</th><th>共购次数</th></tr>'
    cross_items = prod['cross_sell']
    if len(cross_items) >= 2:
        for item in cross_items:
            cross_html += f'<tr><td>{item["product_a"]}</td><td>{item["product_b"]}</td><td>{item["count"]}</td></tr>'
        cross_html += '</table></div>'
    else:
        cross_html = '<p style="color:#999;font-size:13px;padding:20px;">当前数据中多品订单较少（大部分订单仅含1件商品），关联购买分析需更多交易数据支撑。建议在促销活动中推广捆绑购买以积累关联数据。</p>'

    html += f'''<div id="content3" class="tab-content">

<div class="insight-box">产品分析: {len(prod['category'])}个品类中，Top3贡献了{prod['category']['sales'].head(3).sum()/prod['category']['sales'].sum()*100:.1f}%的销售额。建议优化长尾品类结构，同时关注高客单价品类的转化效率。</div>

<div class="chart-row full"><div class="chart-card"><h3>品类销售额排名</h3><div class="plot" id="c3_category"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>品牌销售额 Top 10</h3><div class="plot" id="c3_brand"></div></div>
<div class="chart-card"><h3>品类平均单价对比</h3><div class="plot" id="c3_avgprice"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>价格区间-商品数与销量</h3><div class="plot" id="c3_price_dist"></div></div>
<div class="chart-card"><h3>关联购买 TOP 商品组合</h3>{cross_html}</div></div>
</div>
'''

    # ================================================================
    # TAB 4: User Profile
    # ================================================================

    # Cross table HTML
    cross_table = up['cross'].to_html(classes='data-table', border=0)
    top_province = up['province']['province'].iloc[0]

    html += f'''<div id="content4" class="tab-content">

<div class="insight-box">用户画像: 男女比例{up['gender']['count'].iloc[0]/(up['gender']['count'].sum())*100:.1f}:{up['gender']['count'].iloc[1]/(up['gender']['count'].sum())*100:.1f}，用户年龄集中在25-35岁，Top省份为{top_province}。消费等级分布显示用户结构较为均衡。</div>

<div class="chart-row three"><div class="chart-card"><h3>性别分布</h3><div class="plot" id="c4_gender"></div></div>
<div class="chart-card"><h3>年龄分布</h3><div class="plot" id="c4_age"></div></div>
<div class="chart-card"><h3>会员等级分布</h3><div class="plot" id="c4_member"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>用户地域分布 Top 10</h3><div class="plot" id="c4_province"></div></div>
<div class="chart-card"><h3>消费能力等级分布</h3><div class="plot" id="c4_consumption"></div></div></div>
<div class="chart-row full"><div class="chart-card"><h3>会员等级 × 消费能力交叉分析</h3><div class="table-wrap">{cross_table}</div></div></div>
</div>
'''

    # ================================================================
    # TAB 5: Behavior Analysis
    # ================================================================
    funnel_data = beh['funnel']

    html += f'''<div id="content5" class="tab-content">

<div class="insight-box">用户行为分析: 浏览-加购转化率{funnel_data['rate'].iloc[3]}，浏览-下单转化率{funnel_data['rate'].iloc[4]}。用户在平台的行为以浏览为主({beh['total_behaviors']:,}次)，行为时段分布可指导广告投放和推送时机。</div>

<div class="chart-row full"><div class="chart-card"><h3>用户行为转化漏斗（含转化率）</h3><div class="plot" id="c5_funnel"></div></div></div>
<div class="chart-row full"><div class="chart-card"><h3>日度行为量 vs 营收趋势</h3><div class="plot" id="c5_behavior_revenue"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>时段行为分布（小时级）</h3><div class="plot" id="c5_hourly"></div></div>
<div class="chart-card"><h3>行为时长分布</h3><div class="plot" id="c5_duration"></div></div></div>
<div class="chart-row full"><div class="chart-card"><h3>用户活跃度分布（行为次数区间）</h3><div class="plot" id="c5_activeness"></div></div></div>
</div>
'''

    # ================================================================
    # TAB 6: User Clustering
    # ================================================================
    cp = cl['cluster_profile']
    cluster_table = cp.to_html(classes='data-table', index=False)

    # Build Sankey migration tables
    lc_stages = sankey.get('lc_stages', [])
    lc_matrix = sankey.get('lc_matrix', {})
    lc_migration_rows = ''
    for s1 in lc_stages:
        lc_migration_rows += f'<tr><td><b>P1-{s1}</b></td>'
        p1_total = sum(lc_matrix.get(s1, {}).values())
        for s2 in lc_stages:
            val = lc_matrix.get(s1, {}).get(s2, 0)
            highlight = 'style="color:#667eea;font-weight:bold;"' if s1 == s2 else ''
            lc_migration_rows += f'<td {highlight}>{val:,}</td>'
        lc_migration_rows += f'<td style="color:#667eea;font-weight:bold;">{p1_total:,}</td></tr>\n'

    rfm_stages = sankey.get('rfm_stages', [])
    rfm_matrix = sankey.get('rfm_matrix', {})
    rfm_migration_rows = ''
    for s1 in rfm_stages:
        rfm_migration_rows += f'<tr><td><b>P1-{s1}</b></td>'
        p1_total = sum(rfm_matrix.get(s1, {}).values())
        for s2 in rfm_stages:
            val = rfm_matrix.get(s1, {}).get(s2, 0)
            highlight = 'style="color:#667eea;font-weight:bold;"' if s1 == s2 else ''
            rfm_migration_rows += f'<td {highlight}>{val:,}</td>'
        rfm_migration_rows += f'<td style="color:#667eea;font-weight:bold;">{p1_total:,}</td></tr>\n'

    lc_net = sankey.get('lc_net', {})
    lc_retention = sankey.get('lc_retention', {})
    lc_net_rows = ''
    for s in lc_stages:
        net = lc_net.get(s, 0)
        ret = lc_retention.get(s, 0)
        arrow = f'{chr(8593)}' if net > 0 else (f'{chr(8595)}' if net < 0 else f'{chr(8596)}')
        color = 'color:#27ae60;' if net >= 0 else 'color:#e74c3c;'
        lc_net_rows += f'<tr><td>{s}</td><td style="{color}">{arrow} {abs(net):,}人</td><td>{ret}%</td></tr>\n'

    # Top migration paths
    all_paths = []
    for s1 in lc_stages:
        for s2 in lc_stages:
            if s1 != s2:
                all_paths.append((s1, s2, lc_matrix.get(s1, {}).get(s2, 0)))
    all_paths.sort(key=lambda x: x[2], reverse=True)
    top_paths = all_paths[:3]
    lc_top_paths = ''
    for s1, s2, cnt in top_paths:
        if cnt > 0:
            lc_top_paths += f'<strong>{s1} → {s2}:</strong> {cnt:,}人 | '

    # ================================================================
    # Compute migration analysis insights
    # ================================================================
    total_migrated = sum(lc_net.get(s, 0) for s in lc_stages if lc_net.get(s, 0) > 0)
    total_lost = sum(abs(lc_net.get(s, 0)) for s in lc_stages if lc_net.get(s, 0) < 0)

    # Find the largest migration flows
    all_flows = []
    for s1 in lc_stages:
        for s2 in lc_stages:
            cnt = lc_matrix.get(s1, {}).get(s2, 0)
            if s1 != s2 and cnt > 0:
                all_flows.append((s1, s2, cnt))
    all_flows.sort(key=lambda x: x[2], reverse=True)

    # Find highest retention stage
    best_retention_stage = max(lc_retention, key=lc_retention.get)
    worst_retention_stage = min(lc_retention, key=lc_retention.get)

    # RFM analysis
    rfm_upgrade = 0
    rfm_downgrade = 0
    rfm_stable = 0
    rfm_order = ['无消费', '低价值', '中价值', '高价值']
    for s1 in rfm_stages:
        for s2 in rfm_stages:
            cnt = rfm_matrix.get(s1, {}).get(s2, 0)
            if s1 == s2:
                rfm_stable += cnt
            elif rfm_order.index(s1) < rfm_order.index(s2):
                rfm_upgrade += cnt
            else:
                rfm_downgrade += cnt

    lost_retention = lc_matrix.get('流失用户', {}).get('流失用户', 0)
    low_value_retention = rfm_matrix.get('低价值', {}).get('低价值', 0)

    sankey_analysis_html = f'''<h3 class="section-title" style="margin-top:20px;">{chr(128202)} 用户分层迁移深度分析</h3>

<div class="insight-box"><strong>分析概述:</strong> 将数据按时间均分为P1(前半段)和P2(后半段)两个时段，追踪{total_migrated+total_lost:,}名用户在生命周期阶段的迁移轨迹。P2留存率最高的是<strong>{best_retention_stage}</strong>({lc_retention[best_retention_stage]}%)，最低的是<strong>{worst_retention_stage}</strong>({lc_retention[worst_retention_stage]}%)。RFM维度下，{rfm_upgrade:,}人价值提升，{rfm_downgrade:,}人价值下降。</div>

<h4 style="color:{C['primary']};margin-top:16px;">一、迁移原因分析</h4>
<div class="insight-box">
<strong>1. 新用户→活跃用户转化</strong>: 新用户在首次购买后若获得良好的商品质量和使用体验，大概率转化为活跃用户；但若首单体验不佳(发货延迟、商品不符预期)，则快速流向沉默或流失。<br>
<strong>2. 活跃用户→沉默用户</strong>: 主要驱动力包括: (a) 品类购买周期长—用户完成大件消费后暂无新的购买需求；(b) 竞品分流—竞争对手促销或新品吸引；(c) 平台运营缺失—缺乏个性化推送和会员权益提醒。<br>
<strong>3. 沉默用户→流失用户</strong>: 长期(>90天)无任何行为和订单的用户最终走向流失，核心原因是缺乏有效的召回机制和回归激励。<br>
<strong>4. 消费价值迁移</strong>: 高价值用户的消费金额下降通常与客单价品类变化相关(从高价品类→低价品类)；低价值→中价值的提升路径清晰—提升购买频次是核心驱动力。<br>
<strong>5. 结构性因素</strong>: 数据期覆盖下半年的电商大促(双十一、年货节)，P2时段可能出现集中消费导致的活跃度虚高，需关注P2结束后是否快速回落。
</div>

<h4 style="color:{C['green']};margin-top:12px;">二、迁移结果总结</h4>
<div class="insight-box">
<strong>正面结果:</strong><br>
&bull; 高价值用户留存稳定，该群体品牌忠诚度高，是平台的利润基本盘<br>
&bull; RFM维度升级({rfm_upgrade:,}人) > 降级({rfm_downgrade:,}人)，说明整体用户消费力在报告期内呈上升趋势<br>
&bull; 活跃用户群体在P2得到了有效扩张，得益于新用户的持续转化<br>
<strong style="color:{C['accent']};">风险警报:</strong><br>
&bull; 流失用户({lost_retention:,}人留存)群体规模较大，若不加干预将持续扩大<br>
&bull; 沉默→流失转化率偏高，说明当前的沉默用户召回策略效果有限<br>
&bull; 低价值用户中有{low_value_retention:,}人滞留，未能实现有效的消费升级引导
</div>

<h4 style="color:{C['orange']};margin-top:12px;">三、运营方案建议</h4>
<div class="insight-box">
<strong style="color:{C['accent']};">P0 - 紧急: 流失预警与召回</strong><br>
&bull; <strong>流失预警模型</strong>: 基于RFM和行为频次建立评分卡，将连续30天无行为的用户标记为"高风险"，自动触发APP Push + 短信双重触达<br>
&bull; <strong>阶梯回归券</strong>: 沉默7天送满99减10、沉默30天送满99减20、沉默60天送满59减20+免邮券，步步加码<br>
&bull; <strong>老客专属权益</strong>: 对"高价值→沉默"用户提供VIP客服专线、优先发货等差异化服务，降低高价值用户流失率<br>
&bull; <strong>目标</strong>: 将{worst_retention_stage}留存率从{lc_retention[worst_retention_stage]}%提升至60%，预计可挽回{int(total_lost * 0.3):,}名用户<br><br>

<strong style="color:{C['primary']};">P1 - 重要: 价值升级与转化</strong><br>
&bull; <strong>消费阶梯任务</strong>: 为新用户设计"首单→复购→三单→五单"阶梯任务系统，每完成一阶段发放对应权益，引导从新用户→活跃用户的路径<br>
&bull; <strong>关联品类推荐</strong>: 基于品类购买顺序规律，在高频品类商品详情页推荐关联的高价值品类，提升跨品类消费概率<br>
&bull; <strong>会员成长计划</strong>: 将消费金额与会员等级强关联，设置明确的升级门槛和权益展示，激励中低价值用户向上攀爬<br>
&bull; <strong>首单体验优化</strong>: 新用户首单提供"不满意极速退款"、"延迟发货赔付"等承诺，降低首单决策门槛和退货顾虑<br><br>

<strong style="color:{C['green']};">P2 - 持续: 高价值用户维护</strong><br>
&bull; <strong>VIP专属活动</strong>: 每月举办高价值用户专属的限量发售/品牌联名/0元试用活动，强化身份认同<br>
&bull; <strong>KOC培养计划</strong>: 从高价值用户中筛选KOC种子用户，提供返佣和曝光激励，引导其成为品牌传播节点<br>
&bull; <strong>流失防御机制</strong>: 对高价值用户设置"'即将沉默'预警线"(如15天无行为)，在进入沉默前主动触达，防患于未然
</div>'''

    html += f'''<div id="content6" class="tab-content">

<div class="kpi-row four">
    <div class="kpi-card"><div class="label">用户总数</div><div class="value color-primary">{len(cl['profile']):,}</div></div>
    <div class="kpi-card"><div class="label">聚类数 (K)</div><div class="value color-green">{cl['best_k']}</div></div>
    <div class="kpi-card"><div class="label">轮廓系数</div><div class="value color-orange">{cl['silhouette']:.4f}</div></div>
    <div class="kpi-card"><div class="label">Calinski-Harabasz</div><div class="value color-purple">{cl['calinski']:.0f}</div></div>
</div>

<div class="insight-box">聚类分析: 基于RFM和行为特征将用户分为{cl['best_k']}个群体。各群体在消费金额、活跃度、行为偏好上存在显著差异，为精细化运营提供数据基础。</div>

<div class="insight-box" style="margin-top:12px;text-align:center;">↗️ 详细的<strong>用户分层迁移分析（桑基图）</strong>请查看<strong style="color:#667eea;">「分层迁移」</strong>标签页</div>

<div class="chart-row"><div class="chart-card"><h3>肘部法则</h3><div class="plot" id="c6_elbow"></div></div>
<div class="chart-card"><h3>轮廓系数</h3><div class="plot" id="c6_silhouette"></div></div></div>
<div class="chart-row full"><div class="chart-card"><h3>PCA 聚类可视化</h3><div class="plot" id="c6_pca"></div></div></div>
<div class="chart-row full"><div class="chart-card"><h3>用户群体特征表</h3><div class="table-wrap">{cluster_table}</div></div></div>
<div class="chart-row"><div class="chart-card"><h3>群体数量分布</h3><div class="plot" id="c6_size"></div></div>
<div class="chart-card"><h3>RFM 雷达图对比</h3><div class="plot" id="c6_radar"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>群体性别构成</h3><div class="plot" id="c6_gender"></div></div>
<div class="chart-card"><h3>群体消费等级构成</h3><div class="plot" id="c6_cons"></div></div></div>

<h3 style="margin-top:20px;">群体运营建议</h3>
'''

    for c in range(cl['best_k']):
        row = cp.iloc[c]
        name = row['群体标签']
        cnt = int(row['人数'])
        suggestions = {
            '高价值忠诚用户': '核心用户，高消费高活跃。建议: VIP专属服务、新品首发推送、积分翻倍日、品牌共创邀请。',
            '高消费活跃用户': '高消费中频次。建议: 跨品类推荐、高端商品推送、KOC口碑激励。',
            '高频活跃用户': '高频消费。建议: 满减券提升客单价、签到任务增强互动。',
            '沉睡高价值用户': '曾高消费但近期不活跃。建议: 专属回归券、电话触达、新品通知。',
            '新晋潜力用户': '近期开始活跃。建议: 新用户成长任务、爆款推荐、首月消费奖励。',
        }
        advice = suggestions.get(name, '针对性运营: 分析偏好推送精准推荐，适度优惠激励转化。')
        tag_color = C['cluster'][c]
        pct = cnt/len(cl['profile'])*100
        html += '<div class="insight-box"><strong><span class="tag" style="background:' + str(tag_color) + ';">' + str(name) + '</span> (' + format(cnt, ',') + '人, ' + format(pct, '.1f') + '%)</strong>: ' + str(advice) + '</div>\n'

    html += '</div>\n'


    # ================================================================
    # TAB 7: 用户分层迁移 (Sankey)
    # ================================================================
    html += f'''<div id="content7" class="tab-content">

<!-- ===== SANKET: 用户分层迁移分析 ===== -->
<h3 class="section-title" style="margin-top:20px;">{chr(128259)} 用户分层迁移分析（桑基图）</h3>
<div class="insight-box"><strong>时段对比:</strong> P1 = {sankey.get('periods', {}).get('P1', {}).get('label', '前半段')} | P2 = {sankey.get('periods', {}).get('P2', {}).get('label', '后半段')} — 分析用户在两个等长时段（各约3个月）间的分层迁移，揭示用户生命周期阶段和消费价值的变化趋势。</div>

<div class="chart-row">
<div class="chart-card"><h3>生命周期阶段迁移流</h3><div class="plot" id="c6_sankey_lc"></div>
<div class="table-wrap" style="margin-top:10px;">
<table class="data-table" style="font-size:11px;">
<tr><th>P1 \\ P2</th>
{''.join(f'<th>P2-{s}</th>' for s in sankey.get('lc_stages', []))}
<th style="color:#667eea;">P1总计</th></tr>
{lc_migration_rows}
</table></div></div></div>
<div class="chart-row">
<div class="chart-card"><h3>RFM价值分层迁移流</h3><div class="plot" id="c6_sankey_rfm"></div>
<div class="table-wrap" style="margin-top:10px;">
<table class="data-table" style="font-size:11px;">
<tr><th>P1 \\ P2</th>
{''.join(f'<th>P2-{s}</th>' for s in sankey.get('rfm_stages', []))}
<th style="color:#667eea;">P1总计</th></tr>
{rfm_migration_rows}
</table></div></div></div>

<div class="chart-row">
<div class="chart-card"><h3>生命周期净流量分析</h3>
<div class="table-wrap"><table class="data-table" style="font-size:11px;">
<tr><th>生命周期阶段</th><th>净流入/流出(人)</th><th>P2留存率</th></tr>
{lc_net_rows}
</table></div></div>
<div class="chart-card"><h3>关键迁移路径</h3>
<div class="insight-box" style="margin:4px 0;">
{lc_top_paths}
</div></div></div>

{sankey_analysis_html}

<hr style="margin:20px 0;border:none;border-top:1px solid #e8e8e8;">


    </div>
'''

    # ================================================================
    # TAB 8: Reviews & Service
    # ================================================================
    html += f'''<div id="content8" class="tab-content">

<div class="kpi-row four">
    <div class="kpi-card"><div class="label">平均评分</div><div class="value color-green" id="kpi-avg-review">{rv['avg_review']:.2f}</div></div>
    <div class="kpi-card"><div class="label">平均配送天数</div><div class="value color-primary" id="kpi-avg-delivery">{rv['avg_delivery']:.1f}天</div></div>
    <div class="kpi-card"><div class="label">取消/退款订单</div><div class="value color-accent" id="kpi-cancel-orders">{rv['cancel_by_payment']['count'].sum():,}</div></div>
    <div class="kpi-card"><div class="label">评分≥4占比</div><div class="value color-purple" id="kpi-good-review">{rv['review_dist'].get(4,0)+rv['review_dist'].get(5,0)}单</div></div>
</div>

<div class="insight-box">评价与服务: 整体评分{rv['avg_review']:.2f}分，处于较好水平。物流平均{rv['avg_delivery']:.1f}天送达，建议持续优化配送时效和售后响应速度。</div>

<div class="chart-row"><div class="chart-card"><h3>评价分数分布</h3><div class="plot" id="c7_review"></div></div>
<div class="chart-card"><h3>月度平均评分趋势</h3><div class="plot" id="c7_monthly_review"></div></div></div>
<div class="chart-row"><div class="chart-card"><h3>物流时效分布</h3><div class="plot" id="c7_delivery"></div></div>
<div class="chart-card"><h3>取消/退款-支付方式分布</h3><div class="plot" id="c7_cancel"></div></div></div>
</div>
'''

    # Close tab-wrap
    html += '</div>\n</div>\n'

    html += f'''<div class="footer"><p>天猫用户销售数据 统一分析报表 | 作者: kadima | 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} | AI: Claude Code + DeepSeek v4 Pro | 数据来源: tmall_data MySQL</p></div>\n<script>\n'''

    # Embed monthly data for time filtering
    import json
    monthly_json = json.dumps(monthly, ensure_ascii=False, default=str)
    html += f'var MONTHLY_DATA = {monthly_json};\n'

    # ================================================================
    # ALL PLOTLY CHARTS
    # ================================================================
    charts_js = ""

    # --- TAB 1 CHARTS ---
    # Monthly GMV & Revenue
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(name='GMV(万元)', x=rev['monthly']['order_month'], y=rev['monthly']['GMV'], marker_color='#a0c4ff'), secondary_y=False)
    fig.add_trace(go.Bar(name='营收(万元)', x=rev['monthly']['order_month'], y=rev['monthly']['revenue'], marker_color=C['primary']), secondary_y=False)
    fig.add_trace(go.Scatter(name='订单数', x=rev['monthly']['order_month'], y=rev['monthly']['orders'], mode='lines+markers', marker_color=C['accent'], line=dict(width=2)), secondary_y=True)
    fig.update_layout(template='plotly_white', height=380, hovermode='x unified', legend=dict(orientation='h', y=1.12))
    charts_js += f"Plotly.newPlot('c1_revenue_monthly', {fig.to_json()}, {{responsive: true}});\n"

    # Daily Revenue
    fig = go.Figure()
    fig.add_trace(go.Scatter(name='日营收(万元)', x=rev['daily']['order_date_date'], y=rev['daily']['revenue'], mode='markers+lines', marker=dict(size=3, color='#a0c4ff'), line=dict(width=0.8, color='#e0e0e0')))
    fig.add_trace(go.Scatter(name='7日MA', x=rev['daily']['order_date_date'], y=rev['daily']['ma7'], mode='lines', line=dict(width=2.5, color=C['accent'])))
    fig.update_layout(template='plotly_white', height=350, hovermode='x unified', legend=dict(orientation='h', y=1.12))
    charts_js += f"Plotly.newPlot('c1_revenue_daily', {fig.to_json()}, {{responsive: true}});\n"

    # Orders & AVG
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(name='订单量', x=rev['monthly']['order_month'], y=rev['monthly']['orders'], marker_color=C['primary']), secondary_y=False)
    fig.add_trace(go.Scatter(name='客单价(元)', x=rev['monthly']['order_month'], y=rev['monthly']['avg_order'], mode='lines+markers', marker_color=C['orange'], line=dict(width=2)), secondary_y=True)
    fig.update_layout(template='plotly_white', height=350, legend=dict(orientation='h', y=1.12))
    charts_js += f"Plotly.newPlot('c1_orders_avg', {fig.to_json()}, {{responsive: true}});\n"

    # Discount & ARPU
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(name='折扣率(%)', x=rev['monthly']['order_month'], y=rev['monthly']['discount_rate'], mode='lines+markers', marker=dict(size=8, color=C['accent']), line=dict(width=2)), secondary_y=False)
    fig.add_trace(go.Scatter(name='ARPU(元)', x=rev['monthly']['order_month'], y=rev['monthly']['arpu'], mode='lines+markers', marker=dict(size=8, color=C['purple']), line=dict(width=2)), secondary_y=True)
    fig.update_layout(template='plotly_white', height=350, legend=dict(orientation='h', y=1.12))
    charts_js += f"Plotly.newPlot('c1_discount_arpu', {fig.to_json()}, {{responsive: true}});\n"

    # Payment
    fig = make_subplots(rows=1, cols=2, specs=[[{'type':'pie'}, {'type':'bar'}]])
    fig.add_trace(go.Pie(labels=rev['payment']['payment_method'], values=rev['payment']['count'], hole=0.4, textinfo='label+percent', marker=dict(colors=['#667eea','#764ba2','#e74c3c','#27ae60','#f39c12'])), row=1, col=1)
    fig.add_trace(go.Bar(x=rev['payment']['payment_method'], y=rev['payment']['amount'], marker_color=C['primary'], text=rev['payment']['amount'].round(0), textposition='outside'), row=1, col=2)
    fig.update_layout(template='plotly_white', height=350, showlegend=False)
    fig.update_yaxes(title='金额(万元)', row=1, col=2)
    charts_js += f"Plotly.newPlot('c1_payment', {fig.to_json()}, {{responsive: true}});\n"

    # Status
    fig = go.Figure(go.Pie(labels=rev['status']['order_status'], values=rev['status']['count'], hole=0.4, textinfo='label+percent'))
    fig.update_layout(template='plotly_white', height=350)
    charts_js += f"Plotly.newPlot('c1_status', {fig.to_json()}, {{responsive: true}});\n"

    # --- TAB 2 CHARTS ---
    # DAU trend
    dau = lc['daily_active']
    fig = go.Figure()
    fig.add_trace(go.Scatter(name='DAU', x=dau['date'], y=dau['dau'], mode='lines', line=dict(width=1.5, color=C['primary']), fill='tozeroy', fillcolor='rgba(102,126,234,0.1)'))
    fig.update_layout(template='plotly_white', height=350, yaxis_title='日活跃用户数')
    charts_js += f"Plotly.newPlot('c2_dau_wau_mau', {fig.to_json()}, {{responsive: true}});\n"

    # New vs Lost
    new_df = lc['new_users_monthly']
    fig = go.Figure()
    fig.add_trace(go.Bar(name='新增用户', x=new_df['first_month'], y=new_df['new_users'], marker_color=C['green']))
    fig.update_layout(template='plotly_white', height=350, yaxis_title='用户数')
    charts_js += f"Plotly.newPlot('c2_new_lost', {fig.to_json()}, {{responsive: true}});\n"

    # Lifecycle pie
    ld = lc['lifecycle_dist']
    fig = go.Figure(go.Pie(labels=list(ld.keys()), values=list(ld.values()), hole=0.5, textinfo='label+percent',
                           marker=dict(colors=[C['green'], C['orange'], C['accent']])))
    fig.update_layout(template='plotly_white', height=350)
    charts_js += f"Plotly.newPlot('c2_lifecycle', {fig.to_json()}, {{responsive: true}});\n"

    # Retention heatmap
    rdf = lc['retention_df']
    if len(rdf) > 0:
        weeks = [f'W{w}' for w in range(8)]
        heat_data = rdf[weeks].values
        fig = go.Figure(go.Heatmap(
            z=heat_data, x=[f'第{w}周' for w in range(8)],
            y=rdf['cohort'].values, colorscale='Blues', text=heat_data,
            texttemplate='%{text:.0f}%', textfont=dict(size=10),
            colorbar=dict(title='留存率%')
        ))
        fig.update_layout(template='plotly_white', height=400, xaxis_title='周数', yaxis_title='用户队列')
    charts_js += f"Plotly.newPlot('c2_retention', {fig.to_json()}, {{responsive: true}});\n"

    # Repurchase interval
    intervals = lc['repurchase_intervals']
    interval_sample = intervals[intervals <= 60]
    fig = go.Figure(go.Histogram(x=interval_sample, nbinsx=30, marker_color=C['primary'], name='复购间隔(天)'))
    fig.add_vline(x=lc['avg_repurchase_interval'], line_dash="dash", line_color=C['accent'], annotation_text=f"均值:{lc['avg_repurchase_interval']:.0f}天")
    fig.update_layout(template='plotly_white', height=350, xaxis_title='间隔天数', yaxis_title='频次')
    charts_js += f"Plotly.newPlot('c2_repurchase_interval', {fig.to_json()}, {{responsive: true}});\n"

    # LTV by orders
    ltv_df = lc['ltv_by_orders']
    ltv_df_trim = ltv_df[ltv_df['order_count'] <= 10]
    fig = go.Figure(go.Bar(x=ltv_df_trim['order_count'], y=ltv_df_trim['ltv'], marker_color=C['purple']))
    fig.update_layout(template='plotly_white', height=350, xaxis_title='订单数', yaxis_title='平均LTV(元)')
    charts_js += f"Plotly.newPlot('c2_ltv', {fig.to_json()}, {{responsive: true}});\n"

    # --- TAB 3 CHARTS ---
    cat = prod['category']
    fig = go.Figure(go.Bar(y=cat['category'], x=cat['sales'], orientation='h',
                           marker=dict(color=cat['sales'], colorscale='Blues'), text=cat['sales'].round(0), textposition='outside'))
    fig.update_layout(template='plotly_white', height=420, xaxis_title='销售额(万元)')
    charts_js += f"Plotly.newPlot('c3_category', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Bar(x=prod['brand_top10']['brand'], y=prod['brand_top10']['sales'], marker_color=C['primary'], text=prod['brand_top10']['sales'].round(0), textposition='outside'))
    fig.update_layout(template='plotly_white', height=350, yaxis_title='销售额(万元)')
    charts_js += f"Plotly.newPlot('c3_brand', {fig.to_json()}, {{responsive: true}});\n"

    cat_sorted = cat.sort_values('avg_price', ascending=True)
    fig = go.Figure(go.Bar(y=cat_sorted['category'], x=cat_sorted['avg_price'], orientation='h',
                           marker=dict(color=cat_sorted['avg_price'], colorscale='Oranges'), text=cat_sorted['avg_price'].round(0), textposition='outside'))
    fig.update_layout(template='plotly_white', height=350, xaxis_title='均价(元)')
    charts_js += f"Plotly.newPlot('c3_avgprice', {fig.to_json()}, {{responsive: true}});\n"

    # Price distribution
    pdist = prod['price_dist']
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(name='商品数', x=pdist['price_range'], y=pdist['product_count'], marker_color=C['primary']), secondary_y=False)
    fig.add_trace(go.Scatter(name='总销量', x=pdist['price_range'], y=pdist['total_sales'], mode='lines+markers', marker_color=C['accent'], line=dict(width=2)), secondary_y=True)
    fig.update_layout(template='plotly_white', height=350, legend=dict(orientation='h', y=1.12))
    charts_js += f"Plotly.newPlot('c3_price_dist', {fig.to_json()}, {{responsive: true}});\n"

    # --- TAB 4 CHARTS ---
    fig = go.Figure(go.Pie(labels=up['gender']['gender'], values=up['gender']['count'], hole=0.5, textinfo='label+percent', marker=dict(colors=['#667eea','#e74c3c'])))
    fig.update_layout(template='plotly_white', height=280, showlegend=False)
    charts_js += f"Plotly.newPlot('c4_gender', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Bar(x=up['age_dist']['age_group'], y=up['age_dist']['count'], marker_color=C['secondary'], text=up['age_dist']['count'], textposition='outside'))
    fig.update_layout(template='plotly_white', height=280)
    charts_js += f"Plotly.newPlot('c4_age', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Pie(labels=up['member']['level'], values=up['member']['count'], hole=0.5, textinfo='label+percent'))
    fig.update_layout(template='plotly_white', height=280, showlegend=False)
    charts_js += f"Plotly.newPlot('c4_member', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Bar(x=up['province']['province'], y=up['province']['count'], marker_color=C['primary'], text=up['province']['count'], textposition='outside'))
    fig.update_layout(template='plotly_white', height=350)
    charts_js += f"Plotly.newPlot('c4_province', {fig.to_json()}, {{responsive: true}});\n"

    colors_c = [C['green'], C['orange'], C['accent']]
    fig = go.Figure(go.Pie(labels=up['consumption']['level'], values=up['consumption']['count'], hole=0.5, textinfo='label+percent', marker=dict(colors=colors_c)))
    fig.update_layout(template='plotly_white', height=350, showlegend=False)
    charts_js += f"Plotly.newPlot('c4_consumption', {fig.to_json()}, {{responsive: true}});\n"

    # --- TAB 5 CHARTS ---
    fig = go.Figure(go.Funnel(y=beh['funnel']['stage'], x=beh['funnel']['count'], textinfo='value+percent initial',
                              texttemplate='%{value:,}<br>%{percentInitial:.1%}',
                              marker=dict(color=['#a0c4ff', C['primary'], C['secondary'], C['accent'], C['green']])))
    fig.update_layout(template='plotly_white', height=380)
    charts_js += f"Plotly.newPlot('c5_funnel', {fig.to_json()}, {{responsive: true}});\n"

    # Behavior vs Revenue
    daily_rev = rev['daily']
    daily_beh = beh['daily_behaviors']
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(name='日行为量', x=daily_beh['date'], y=daily_beh['behavior_count'], mode='lines', line=dict(width=1.5, color=C['primary'])), secondary_y=False)
    fig.add_trace(go.Scatter(name='日营收(万元)', x=daily_rev['order_date_date'], y=daily_rev['revenue'], mode='lines', line=dict(width=1.5, color=C['accent'])), secondary_y=True)
    fig.update_layout(template='plotly_white', height=350, hovermode='x unified', legend=dict(orientation='h', y=1.12))
    charts_js += f"Plotly.newPlot('c5_behavior_revenue', {fig.to_json()}, {{responsive: true}});\n"

    # Hourly
    fig = go.Figure(go.Bar(x=beh['hourly']['hour'], y=beh['hourly']['count'], marker_color=C['primary']))
    fig.update_layout(template='plotly_white', height=350, xaxis_title='小时', yaxis_title='行为量', xaxis=dict(dtick=2))
    charts_js += f"Plotly.newPlot('c5_hourly', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Bar(x=beh['duration_dist']['range'], y=beh['duration_dist']['count'], marker_color=C['secondary']))
    fig.update_layout(template='plotly_white', height=350, xaxis_title='时长范围', yaxis_title='行为量')
    charts_js += f"Plotly.newPlot('c5_duration', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Bar(x=beh['activeness_dist']['range'], y=beh['activeness_dist']['count'], marker_color=C['orange'], text=beh['activeness_dist']['count'], textposition='outside'))
    fig.update_layout(template='plotly_white', height=350, xaxis_title='行为次数区间', yaxis_title='用户数')
    charts_js += f"Plotly.newPlot('c5_activeness', {fig.to_json()}, {{responsive: true}});\n"

    # --- SANKEY DIAGRAMS (Tab 7) ---
    # Lifecycle Sankey
    if sankey.get('lc_source'):
        fig = go.Figure(go.Sankey(
            arrangement='snap',
            node=dict(
                pad=18, thickness=22,
                line=dict(color='black', width=0.5),
                label=sankey['lc_labels'],
                color=['#667eea','#e74c3c','#f39c12','#95a5a6','#667eea','#e74c3c','#f39c12','#95a5a6']
            ),
            link=dict(
                source=sankey['lc_source'], target=sankey['lc_target'], value=sankey['lc_value'],
                color=[f'rgba(102,126,234,{0.2+v/max(sankey["lc_value"])*0.6})' for v in sankey['lc_value']]
            )
        ))
        fig.update_layout(template='plotly_white', height=420, width=None,
                          title='用户生命周期阶段迁移 (P1→P2)',
                          margin=dict(l=10, r=10, t=40, b=10))
        charts_js += f"Plotly.newPlot('c6_sankey_lc', {fig.to_json()}, {{responsive: true}});\n"

    # RFM Sankey
    if sankey.get('rfm_source'):
        fig = go.Figure(go.Sankey(
            arrangement='snap',
            node=dict(
                pad=18, thickness=22,
                line=dict(color='black', width=0.5),
                label=sankey['rfm_labels'],
                color=['#27ae60','#f39c12','#e74c3c','#95a5a6','#27ae60','#f39c12','#e74c3c','#95a5a6']
            ),
            link=dict(
                source=sankey['rfm_source'], target=sankey['rfm_target'], value=sankey['rfm_value'],
                color=[f'rgba(39,174,96,{0.2+v/max(sankey["rfm_value"])*0.6})' for v in sankey['rfm_value']]
            )
        ))
        fig.update_layout(template='plotly_white', height=420, width=None,
                          title='RFM价值分层迁移 (P1→P2)',
                          margin=dict(l=10, r=10, t=40, b=10))
        charts_js += f"Plotly.newPlot('c6_sankey_rfm', {fig.to_json()}, {{responsive: true}});\n"

    # --- TAB 6 CHARTS ---
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(cl['k_range']), y=cl['inertias'], mode='lines+markers', marker=dict(size=10, color=C['primary']), line=dict(width=2)))
    fig.add_vline(x=cl['best_k'], line_dash="dash", line_color=C['accent'])
    fig.update_layout(template='plotly_white', height=350, xaxis_title='K', xaxis=dict(dtick=1), yaxis_title='Inertia')
    charts_js += f"Plotly.newPlot('c6_elbow', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(cl['k_range']), y=cl['sil_scores'], mode='lines+markers', marker=dict(size=10, color=C['green']), line=dict(width=2)))
    fig.add_vline(x=cl['best_k'], line_dash="dash", line_color=C['accent'])
    fig.update_layout(template='plotly_white', height=350, xaxis_title='K', xaxis=dict(dtick=1), yaxis_title='Silhouette')
    charts_js += f"Plotly.newPlot('c6_silhouette', {fig.to_json()}, {{responsive: true}});\n"

    # PCA
    profile_df = cl['profile']
    fig = go.Figure()
    for c in range(cl['best_k']):
        subset = profile_df[profile_df['cluster'] == c]
        fig.add_trace(go.Scatter(x=subset['pca_x'], y=subset['pca_y'], mode='markers', name=f'{cl["cluster_names"][c]}', marker=dict(size=3, color=C['cluster'][c], opacity=0.5)))
    fig.update_layout(template='plotly_white', height=450, xaxis_title=f'PC1 ({cl["pca_var"][0]:.1f}%)', yaxis_title=f'PC2 ({cl["pca_var"][1]:.1f}%)')
    charts_js += f"Plotly.newPlot('c6_pca', {fig.to_json()}, {{responsive: true}});\n"

    # Cluster sizes
    sizes_data = cp[['群体标签', '人数']].copy()
    fig = go.Figure(go.Bar(y=sizes_data['群体标签'], x=sizes_data['人数'], orientation='h', marker=dict(color=C['cluster']), text=sizes_data['人数'], textposition='outside'))
    fig.update_layout(template='plotly_white', height=260)
    charts_js += f"Plotly.newPlot('c6_size', {fig.to_json()}, {{responsive: true}});\n"

    # Radar
    rfm_cp = cp[['群体标签', 'R值', 'F值', 'M值']].copy()
    for col in ['R值', 'F值', 'M值']:
        mx, mn = rfm_cp[col].max(), rfm_cp[col].min()
        if mx > mn:
            rfm_cp[col] = (rfm_cp[col] - mn) / (mx - mn)
    rfm_cp['R值'] = 1 - rfm_cp['R值']

    fig = go.Figure()
    for i in range(cl['best_k']):
        row = rfm_cp.iloc[i]
        fig.add_trace(go.Scatterpolar(r=[row['R值'], row['F值'], row['M值'], row['R值']],
                                       theta=['新近度(R)', '频率(F)', '金额(M)', '新近度(R)'],
                                       name=row['群体标签'], fill='toself', marker=dict(color=C['cluster'][i]), opacity=0.4))
    fig.update_layout(template='plotly_white', height=380, polar=dict(radialaxis=dict(range=[0, 1])))
    charts_js += f"Plotly.newPlot('c6_radar', {fig.to_json()}, {{responsive: true}});\n"

    # Gender by cluster
    gender_cols = [c for c in cp.columns if '_占比' in c and not c.startswith('消费')]
    if gender_cols:
        fig = go.Figure()
        for i in range(cl['best_k']):
            row = cp.iloc[i]
            fig.add_trace(go.Bar(name=row['群体标签'], x=[c.replace('_占比', '') for c in gender_cols], y=[row[c] for c in gender_cols], marker_color=C['cluster'][i]))
        fig.update_layout(template='plotly_white', height=350, barmode='group', yaxis_title='占比(%)')
    charts_js += f"Plotly.newPlot('c6_gender', {fig.to_json()}, {{responsive: true}});\n"

    # Cons by cluster
    cons_cols = [c for c in cp.columns if c.startswith('消费')]
    if cons_cols:
        fig = go.Figure()
        for i in range(cl['best_k']):
            row = cp.iloc[i]
            fig.add_trace(go.Bar(name=row['群体标签'], x=[c.replace('消费', '').replace('_占比', '') for c in cons_cols], y=[row[c] for c in cons_cols], marker_color=C['cluster'][i]))
        fig.update_layout(template='plotly_white', height=350, barmode='group', yaxis_title='占比(%)')
    charts_js += f"Plotly.newPlot('c6_cons', {fig.to_json()}, {{responsive: true}});\n"

    # --- TAB 7 CHARTS ---
    fig = go.Figure(go.Bar(x=rv['review_dist'].index.astype(int), y=rv['review_dist'].values, marker_color=C['orange'], text=rv['review_dist'].values, textposition='outside'))
    fig.update_layout(template='plotly_white', height=350, xaxis=dict(dtick=1), xaxis_title='评分', yaxis_title='订单数')
    charts_js += f"Plotly.newPlot('c7_review', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Scatter(name='月均评分', x=rv['monthly_review']['month'], y=rv['monthly_review']['avg_score'], mode='lines+markers', marker=dict(size=8, color=C['green']), line=dict(width=2)))
    fig.update_layout(template='plotly_white', height=350, yaxis_title='平均评分', yaxis=dict(range=[4, 5]))
    charts_js += f"Plotly.newPlot('c7_monthly_review', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Bar(x=rv['delivery_dist']['range'], y=rv['delivery_dist']['count'], marker_color=C['primary'], text=rv['delivery_dist']['count'], textposition='outside'))
    fig.update_layout(template='plotly_white', height=350, xaxis_title='配送天数', yaxis_title='订单数')
    charts_js += f"Plotly.newPlot('c7_delivery', {fig.to_json()}, {{responsive: true}});\n"

    fig = go.Figure(go.Bar(x=rv['cancel_by_payment']['payment_method'], y=rv['cancel_by_payment']['count'], marker_color=C['accent']))
    fig.update_layout(template='plotly_white', height=350, xaxis_title='支付方式', yaxis_title='取消/退款订单数')
    charts_js += f"Plotly.newPlot('c7_cancel', {fig.to_json()}, {{responsive: true}});\n"

    # Tab switch resize handler
    charts_js += """
document.querySelectorAll('.tab-wrap input[type=radio]').forEach(function(radio) {
    radio.addEventListener('change', function() {
        setTimeout(function() {
            var plots = document.querySelectorAll('.js-plotly-plot');
            for (var i = 0; i < plots.length; i++) {
                if (plots[i].offsetParent !== null) {
                    Plotly.Plots.resize(plots[i]);
                }
            }
        }, 100);
    });
});

// ============================================================
// Range Slider — Date Filter
// ============================================================
var months = MONTHLY_DATA.months;
var nMonths = months.length;

// Build slider labels
var labelDiv = document.getElementById('slider-labels');
labelDiv.innerHTML = '';
for (var i = 0; i < nMonths; i++) {
    var span = document.createElement('span');
    span.textContent = months[i];
    span.style.fontSize = '10px';
    labelDiv.appendChild(span);
}

// Initial fill
updateSliderFill();

function updateSliderFill() {
    var s = parseInt(document.getElementById('range-start').value);
    var e = parseInt(document.getElementById('range-end').value);
    var pctL = (s / (nMonths - 1)) * 100;
    var pctR = 100 - (e / (nMonths - 1)) * 100;
    document.getElementById('slider-fill').style.left = pctL + '%';
    document.getElementById('slider-fill').style.right = pctR + '%';
    document.getElementById('slider-start-disp').textContent = months[s];
    document.getElementById('slider-end-disp').textContent = months[e];

    if (s <= e) {
        document.getElementById('range-start').value = Math.min(s, e);
        document.getElementById('range-end').value = Math.max(s, e);
    }
}

function onSliderChange() {
    var s = parseInt(document.getElementById('range-start').value);
    var e = parseInt(document.getElementById('range-end').value);
    if (s > e) {
        var tmp = s; s = e; e = tmp;
        document.getElementById('range-start').value = s;
        document.getElementById('range-end').value = e;
    }
    updateSliderFill();
    applyTimeFilter();
}

function getFilteredMonths() {
    var s = parseInt(document.getElementById('range-start').value);
    var e = parseInt(document.getElementById('range-end').value);
    return months.slice(s, e + 1);
}

function sumRange(arr, filtered) {
    var total = 0;
    for (var i = 0; i < nMonths; i++) {
        if (filtered.indexOf(months[i]) >= 0) total += (arr[i] || 0);
    }
    return total;
}

function avgRange(arr, filtered) {
    var total = 0, count = 0;
    for (var i = 0; i < nMonths; i++) {
        if (filtered.indexOf(months[i]) >= 0) { total += (arr[i] || 0); count++; }
    }
    return count > 0 ? total / count : 0;
}

function applyTimeFilter() {
    var filtered = getFilteredMonths();
    var info = document.getElementById('filter-info');

    if (filtered.length === nMonths) {
        info.textContent = '当前: 全部时段 (' + nMonths + '个月)';
    } else {
        info.textContent = '当前: ' + filtered[0] + ' ~ ' + filtered[filtered.length-1] + ' (' + filtered.length + '个月)';
    }

    // Update KPI cards
    var gmv = sumRange(MONTHLY_DATA.gmv, filtered);
    var revenue = sumRange(MONTHLY_DATA.revenue, filtered);
    var orders = sumRange(MONTHLY_DATA.orders, filtered);
    var aov = orders > 0 ? revenue * 10000 / orders : 0;
    var discountRate = avgRange(MONTHLY_DATA.discount_rate, filtered);
    var dau = avgRange(MONTHLY_DATA.dau_mean, filtered);
    var stickiness = avgRange(MONTHLY_DATA.stickiness, filtered);

    var elGmv = document.getElementById('kpi-gmv');
    if (elGmv) elGmv.textContent = gmv.toLocaleString('en-US', {maximumFractionDigits: 0});
    var elRev = document.getElementById('kpi-revenue');
    if (elRev) elRev.textContent = revenue.toLocaleString('en-US', {maximumFractionDigits: 0});
    var elOrd = document.getElementById('kpi-orders');
    if (elOrd) elOrd.textContent = orders.toLocaleString('en-US', {maximumFractionDigits: 0});
    var elAov = document.getElementById('kpi-aov');
    if (elAov) elAov.textContent = aov.toLocaleString('en-US', {maximumFractionDigits: 0});
    var elDisc = document.getElementById('kpi-discount');
    if (elDisc) elDisc.textContent = discountRate.toFixed(1) + '%';
    var uniqueCustomers = 0;
    for (var i = 0; i < nMonths; i++) {
        if (filtered.indexOf(months[i]) >= 0) uniqueCustomers += MONTHLY_DATA.unique_customers[i];
    }
    var repurchaseRate = orders > uniqueCustomers ? (orders / uniqueCustomers * 100).toFixed(1) : '0.0';
    var elRep = document.getElementById('kpi-repurchase');
    if (elRep) elRep.textContent = repurchaseRate + '%';
    var elDau = document.getElementById('kpi-dau');
    if (elDau) elDau.textContent = dau.toLocaleString('en-US', {maximumFractionDigits: 0});
    var elStk = document.getElementById('kpi-stickiness');
    if (elStk) elStk.textContent = stickiness.toFixed(1) + '%';

    updateTimeSeriesCharts(filtered);
}

function updateTimeSeriesCharts(filtered) {
    // --- Tab 1: Revenue ---
    var fGmv = [], fRevenue = [], fOrders = [], fAvgOrder = [], fDiscountRate = [], fArpu = [];
    for (var i = 0; i < nMonths; i++) {
        if (filtered.indexOf(months[i]) >= 0) {
            fGmv.push(MONTHLY_DATA.gmv[i]);
            fRevenue.push(MONTHLY_DATA.revenue[i]);
            fOrders.push(MONTHLY_DATA.orders[i]);
            fAvgOrder.push(MONTHLY_DATA.avg_order[i]);
            fDiscountRate.push(MONTHLY_DATA.discount_rate[i]);
            fArpu.push(MONTHLY_DATA.arpu[i]);
        }
    }
    try { Plotly.update('c1_revenue_monthly', { x: [filtered, filtered, filtered], y: [fGmv, fRevenue, fOrders] }, {}, [0, 1, 2]); } catch(e) {}
    try { Plotly.update('c1_orders_avg', { x: [filtered, filtered], y: [fOrders, fAvgOrder] }, {}, [0, 1]); } catch(e) {}
    try { Plotly.update('c1_discount_arpu', { x: [filtered, filtered], y: [fDiscountRate, fArpu] }, {}, [0, 1]); } catch(e) {}

    // --- Tab 2: Lifecycle ---
    var fNewUsers = [], fLostUsers = [];
    for (var i = 0; i < nMonths; i++) {
        if (filtered.indexOf(months[i]) >= 0) {
            fNewUsers.push(MONTHLY_DATA.new_users[i] || 0);
            fLostUsers.push(MONTHLY_DATA.lost_users[i] || 0);
        }
    }
    try { Plotly.update('c2_new_lost', { x: [filtered], y: [fNewUsers] }, {}, [0]); } catch(e) {}

    // Update lifecycle KPI cards
    var elInterval = document.getElementById('kpi-repurchase-interval');
    if (elInterval) { elInterval.textContent = '--'; }
    var elLtv = document.getElementById('kpi-ltv');
    if (elLtv) { elLtv.textContent = '--'; }

    // --- Tab 7: Reviews ---
    var fReviewScores = [];
    for (var i = 0; i < nMonths; i++) {
        if (filtered.indexOf(months[i]) >= 0) {
            fReviewScores.push(MONTHLY_DATA.review_scores[i] || 0);
        }
    }
    try { Plotly.update('c7_monthly_review', { x: [filtered], y: [fReviewScores] }, {}, [0]); } catch(e) {}

    // Update review KPI cards
    var avgScore = avgRange(MONTHLY_DATA.review_scores, filtered);
    var elAvgRev = document.getElementById('kpi-avg-review');
    if (elAvgRev) elAvgRev.textContent = avgScore.toFixed(2);

    // --- Daily charts: filter by x-axis date range ---
    var firstMonth = filtered[0];
    var lastMonth = filtered[filtered.length - 1];
    var startDate = MONTHLY_DATA.month_dates[firstMonth].start;
    var endDate = MONTHLY_DATA.month_dates[lastMonth].end;

    if (filtered.length === nMonths) {
        // Reset to full range
        try { Plotly.relayout('c1_revenue_daily', { 'xaxis.autorange': true }); } catch(e) {}
        try { Plotly.relayout('c2_dau_wau_mau', { 'xaxis.autorange': true }); } catch(e) {}
        try { Plotly.relayout('c5_behavior_revenue', { 'xaxis.autorange': true }); } catch(e) {}
    } else {
        try { Plotly.relayout('c1_revenue_daily', { 'xaxis.range': [startDate, endDate] }); } catch(e) {}
        try { Plotly.relayout('c2_dau_wau_mau', { 'xaxis.range': [startDate, endDate] }); } catch(e) {}
        try { Plotly.relayout('c5_behavior_revenue', { 'xaxis.range': [startDate, endDate] }); } catch(e) {}
    }
}

function resetTimeFilter() {
    document.getElementById('range-start').value = 0;
    document.getElementById('range-end').value = nMonths - 1;
    updateSliderFill();
    applyTimeFilter();
}

"""

    html += charts_js
    html += '\n</script>\n</body>\n</html>'

    return html


# ================================================================
# MAIN
# ================================================================
if __name__ == '__main__':
    print("=" * 60)
    print("天猫数据分析 - 统一报表生成")
    print("=" * 60)

    print("\n[1/9] 提取数据...")
    users, orders, products, behaviors, features = fetch_all_data()
    users, orders, products, behaviors, features, order_product = prepare_all_data(users, orders, products, behaviors, features)
    print(f"  数据加载完成: {len(users)}用户, {len(orders)}订单, {len(products)}产品, {len(behaviors)}行为")

    print("[2/9] 营收指标计算...")
    revenue_metrics = compute_revenue_metrics(orders)

    print("[3/9] 用户生命周期分析...")
    lifecycle_metrics = compute_user_lifecycle(users, orders, behaviors)
    print(f"  DAU均值: {lifecycle_metrics['daily_active']['dau'].mean():.0f}, 粘性: {lifecycle_metrics['stickiness']:.1f}%")

    print("[4/9] 产品指标计算...")
    product_metrics = compute_product_metrics(order_product, products, orders)

    print("[5/9] 用户画像统计...")
    user_profile_data = compute_user_profile(users, features)

    print("[6/9] 行为指标计算...")
    behavior_metrics = compute_behavior_metrics(behaviors, orders)

    print("[7/9] 用户聚类分析 (K-Means)...")
    clustering_data = compute_clustering(users, orders, features, behaviors)
    print(f"  K={clustering_data['best_k']}, Silhouette={clustering_data['silhouette']:.4f}")

    print("[8/9] 评价与服务分析...")
    review_metrics = compute_review_metrics(orders)

    print("[9/9] 月度数据预计算 + 桑基图分析...")
    monthly_data = compute_monthly_data(orders, behaviors)
    sankey_data = compute_sankey_data(users, orders, behaviors)
    print(f"  月度数据: {len(monthly_data['months'])}个月, 桑基图: {sankey_data['total_users']}用户迁移")

    print("[10/10] 生成统一HTML报表...")
    all_data = {
        'revenue': revenue_metrics, 'lifecycle': lifecycle_metrics,
        'product': product_metrics, 'user_profile': user_profile_data,
        'behavior': behavior_metrics, 'clustering': clustering_data,
        'review': review_metrics, 'products': products,
        'monthly': monthly_data, 'sankey': sankey_data
    }
    html = generate_unified_html(all_data)

    output_path = r'D:\softdata\claude\test002\unified_report.html'
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    import os
    size_kb = os.path.getsize(output_path) / 1024
    print(f"\n统一报表已生成: {output_path}")
    print(f"文件大小: {size_kb:.0f} KB")
    print("=" * 60)
