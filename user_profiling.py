# -*- coding: utf-8 -*-
"""
天猫用户画像分析与聚类分析
使用 sklearn 进行 K-Means 聚类，构建用户画像，结果写入 MySQL
"""
import pymysql
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score, calinski_harabasz_score, davies_bouldin_score
from user_auth_db import get_connection  # 数据库连接配置，请修改 user_auth_db.py 中的密码

# ============================================================
# 1. 数据提取与特征工程
# ============================================================
def fetch_and_engineer():
    conn = get_connection()

    users = pd.read_sql("SELECT * FROM users", conn)
    orders = pd.read_sql("SELECT * FROM orders", conn)
    features = pd.read_sql("SELECT * FROM user_features", conn)
    behaviors = pd.read_sql("SELECT * FROM user_behaviors", conn)
    products = pd.read_sql("SELECT * FROM products", conn)

    conn.close()

    # ---- RFM 特征工程 ----
    orders['order_date_dt'] = pd.to_datetime(orders['order_date_date'])
    ref_date = orders['order_date_dt'].max() + pd.Timedelta(days=1)

    rfm = orders.groupby('user_id').agg(
        Recency=('order_date_dt', lambda x: (ref_date - x.max()).days),
        Frequency=('order_id', 'count'),
        Monetary=('actual_payment', 'sum')
    ).reset_index()
    rfm.columns = ['user_id', 'Recency', 'Frequency', 'Monetary']

    # ---- 行为特征 ----
    behavior_pivot = behaviors.pivot_table(
        index='user_id', columns='behavior_type', values='behavior_id',
        aggfunc='count', fill_value=0
    ).reset_index()

    # ---- 合并特征 ----
    profile = users.merge(features, on='user_id', how='left')
    profile = profile.merge(rfm, on='user_id', how='left')
    if behavior_pivot is not None and len(behavior_pivot.columns) > 1:
        profile = profile.merge(behavior_pivot, on='user_id', how='left')

    # 填充缺失
    for col in ['Recency', 'Frequency', 'Monetary']:
        profile[col] = profile[col].fillna(0)
    for col in ['浏览', '点击', '收藏', '加购']:
        if col in profile.columns:
            profile[col] = profile[col].fillna(0).astype(int)

    # ---- 创建衍生特征 ----
    profile['avg_order_value'] = np.where(profile['Frequency'] > 0,
                                          profile['Monetary'] / profile['Frequency'], 0)
    profile['is_active'] = (profile['Recency'] <= 30).astype(int)
    profile['has_purchased'] = (profile['Frequency'] > 0).astype(int)

    return users, orders, features, behaviors, products, profile, rfm


# ============================================================
# 2. 用户聚类分析
# ============================================================
def perform_clustering(profile):
    """使用 K-Means 进行用户聚类"""
    results = {}

    # ---- 选择聚类特征 ----
    cluster_cols = ['total_spent', 'order_count', 'avg_order_amount',
                    'browse_count', 'click_count', 'favorite_count', 'cart_count',
                    'Recency', 'Frequency', 'Monetary', 'purchase_intent', 'member_level_score']

    # 确保所有列存在
    available_cols = [c for c in cluster_cols if c in profile.columns]
    cluster_data = profile[available_cols].fillna(0)

    # ---- 标准化 ----
    scaler = StandardScaler()
    scaled_data = scaler.fit_transform(cluster_data)

    # ---- 确定最佳K值 ----
    k_range = range(2, 9)
    inertias = []
    sil_scores = []
    for k in k_range:
        km = KMeans(n_clusters=k, random_state=42, n_init=10, max_iter=300)
        labels = km.fit_predict(scaled_data)
        inertias.append(km.inertia_)
        sil_scores.append(silhouette_score(scaled_data, labels))

    results['k_range'] = list(k_range)
    results['inertias'] = inertias
    results['sil_scores'] = sil_scores

    # 选择最佳K: 业务场景优先使用K=5，兼顾Silhouette
    best_sil_k = k_range[np.argmax(sil_scores)]
    # 对于用户分群，K=4~6更适合业务应用
    business_k = 5
    best_k = business_k if business_k in k_range else best_sil_k
    results['best_k'] = best_k
    results['silhouette_k'] = best_sil_k

    # ---- 最终聚类 ----
    final_km = KMeans(n_clusters=best_k, random_state=42, n_init=10, max_iter=300)
    profile['cluster'] = final_km.fit_predict(scaled_data)

    results['silhouette'] = silhouette_score(scaled_data, profile['cluster'])
    results['calinski'] = calinski_harabasz_score(scaled_data, profile['cluster'])
    results['davies'] = davies_bouldin_score(scaled_data, profile['cluster'])

    # ---- PCA 降维可视化 ----
    pca = PCA(n_components=2, random_state=42)
    pca_result = pca.fit_transform(scaled_data)
    profile['pca_x'] = pca_result[:, 0]
    profile['pca_y'] = pca_result[:, 1]
    results['pca_var1'] = pca.explained_variance_ratio_[0] * 100
    results['pca_var2'] = pca.explained_variance_ratio_[1] * 100

    # ---- 聚类画像 ----
    cluster_profile = profile.groupby('cluster').agg(
        人数=('user_id', 'count'),
        平均年龄=('age', 'mean'),
        平均余额=('account_balance', 'mean'),
        平均信用分=('credit_score', 'mean'),
        总消费=('total_spent', 'mean'),
        订单数=('order_count', 'mean'),
        客单价=('avg_order_amount', 'mean'),
        浏览次数=('browse_count', 'mean'),
        收藏次数=('favorite_count', 'mean'),
        加购次数=('cart_count', 'mean'),
        R值=('Recency', 'mean'),
        F值=('Frequency', 'mean'),
        M值=('Monetary', 'mean'),
    ).round(1)

    # 性别分布
    gender_dist = profile.groupby(['cluster', 'gender']).size().unstack(fill_value=0)
    for g in gender_dist.columns:
        cluster_profile[f'{g}_占比'] = (gender_dist[g] / gender_dist.sum(axis=1) * 100).round(1)

    # 消费等级
    if 'consumption_level' in profile.columns:
        cons_dist = profile.groupby(['cluster', 'consumption_level']).size().unstack(fill_value=0)
        for l in cons_dist.columns:
            cluster_profile[f'消费{l}_占比'] = (cons_dist[l] / cons_dist.sum(axis=1) * 100).round(1)

    results['cluster_profile'] = cluster_profile

    # ---- 聚类命名：基于RFM综合评分确保唯一标签 ----
    cluster_names = {}
    # 计算每个聚类的综合评分 (R逆向: R越低越好; F和M正向)
    r_rank = cluster_profile['R值'].rank(ascending=True)   # R低=好, rank低
    f_rank = cluster_profile['F值'].rank(ascending=False)  # F高=好, rank低
    m_rank = cluster_profile['M值'].rank(ascending=False)  # M高=好, rank低
    total_rank = r_rank + f_rank + m_rank
    # 按总分排序: 总分最低=排名最好
    sorted_clusters = total_rank.sort_values().index.tolist()

    name_labels = ['高价值忠诚用户', '高消费活跃用户', '高频活跃用户',
                   '沉睡高价值用户', '新晋潜力用户', '流失预警用户', '一般用户']

    for idx, c in enumerate(sorted_clusters):
        if idx < len(name_labels):
            cluster_names[c] = name_labels[idx]
        else:
            cluster_names[c] = f'用户群体{idx+1}'

    cluster_profile.insert(0, '群体标签', cluster_profile.index.map(cluster_names))
    profile['cluster_name'] = profile['cluster'].map(cluster_names)
    results['cluster_names'] = cluster_names

    return profile, results, scaled_data


# ============================================================
# 3. 主流程
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("天猫用户画像分析与聚类")
    print("=" * 60)

    print("\n[1/3] 提取数据并特征工程...")
    users, orders, features, behaviors, products, profile, rfm = fetch_and_engineer()
    print(f"  - 用户数: {len(profile)}")
    print(f"  - 特征维度: {profile.shape[1]}")

    print("\n[2/3] 执行 K-Means 聚类...")
    profile, results, scaled_data = perform_clustering(profile)
    print(f"  - 最佳K值: {results['best_k']}")
    print(f"  - 轮廓系数: {results['silhouette']:.4f}")
    print(f"  - Calinski-Harabasz: {results['calinski']:.0f}")
    print(f"  - Davies-Bouldin: {results['davies']:.4f}")

    print("\n[3/3] 生成群体画像并保存到数据库...")
    for c in range(results['best_k']):
        row = results['cluster_profile'].iloc[c]
        print(f"  群体{c}: {results['cluster_names'][c]} - {int(row['人数'])}人")

    # Save clustering results to database for further use
    print("\n保存聚类结果到数据库...")
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS claude_user_clusters")
    cur.execute("""
        CREATE TABLE claude_user_clusters (
            user_id VARCHAR(255) PRIMARY KEY,
            cluster INT,
            cluster_name VARCHAR(100),
            recency INT,
            frequency INT,
            monetary DOUBLE,
            pca_x DOUBLE,
            pca_y DOUBLE
        )
    """)

    insert_data = profile[['user_id', 'cluster', 'cluster_name', 'Recency', 'Frequency', 'Monetary', 'pca_x', 'pca_y']].copy()
    for _, row in insert_data.iterrows():
        cur.execute(
            "INSERT INTO claude_user_clusters (user_id, cluster, cluster_name, recency, frequency, monetary, pca_x, pca_y) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (row['user_id'], int(row['cluster']), row['cluster_name'], int(row['Recency']), int(row['Frequency']), float(row['Monetary']), float(row['pca_x']), float(row['pca_y']))
        )
    conn.commit()
    conn.close()
    print(f"  - 已保存 {len(insert_data)} 条聚类结果到 claude_user_clusters")

    print("\n" + "=" * 60)
    print("用户画像分析完成!")
    print("=" * 60)
