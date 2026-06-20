# -*- coding: utf-8 -*-
"""
============================================================
数据库连接配置文件 (用户隐私配置)
============================================================
注意: 此文件包含敏感的数据库连接信息，请勿上传到公开仓库。
其他用户使用前请修改此文件中的连接参数为实际值。

使用方法:
    from user_auth_db import DB_CONFIG, get_connection

    # 方式1: 直接获取连接
    conn = get_connection()

    # 方式2: 使用配置字典
    conn = pymysql.connect(**DB_CONFIG)

配置项说明:
    host     - 数据库主机地址，本地开发用 localhost
    port     - 数据库端口，MySQL默认3306
    user     - 数据库用户名
    password - 数据库密码 (请替换为实际密码)
    database - 数据库名称
    charset  - 字符集，推荐 utf8mb4
============================================================
"""

# ============================================================
# 数据库连接配置 (请修改为实际值)
# ============================================================
DB_CONFIG = {
    'host': 'localhost',
    'port': 3306,
    'user': 'root',
    'password': 'YOUR_PASSWORD_HERE',  # 请替换为实际数据库密码
    'database': 'tmall_data',
    'charset': 'utf8mb4',
}


def get_connection():
    """
    获取数据库连接。
    使用前请确保已修改 DB_CONFIG 中的 password 为实际密码。

    返回:
        pymysql.Connection: 数据库连接对象
    """
    import pymysql
    return pymysql.connect(**DB_CONFIG)
