# -*- coding: utf-8 -*-
"""
============================================================
天猫用户销售数据分析平台 — 主入口 (Main Entry Point)
============================================================

一键运行整个项目，依次执行：
  1. 用户画像分析与聚类 (user_profiling.py)
  2. 统一 BI 报表 (unified_report.py)
  3. 数据探索报告 (generate_exploration_report.py)
  4. PRD 产品需求文档 (generate_prd.py)
  5. 项目总结报告 (generate_summary.py)
  6. 未来扩展规划 (generate_future_plan.py)

输出文件：
  - HTML: unified_report.html
  - Word: tmall_exploration_report.docx / tmall_prd.docx / tmall_summary.docx / tmall_future_plan.docx

============================================================
使用方法：
  python main.py              # 运行所有脚本（默认）
  python main.py --step       # 逐步确认模式
  python main.py --check      # 仅检查环境和数据库连接
============================================================
"""

import os
import sys
import time
import argparse

# 项目根目录
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)

# 需要运行的脚本列表 (按依赖顺序)
SCRIPTS = [
    {
        'name': '用户画像分析与聚类',
        'file': 'user_profiling.py',
        'output': None,
        'description': 'K-Means 聚类分析 + 结果写入 MySQL (claude_user_clusters)',
    },
    {
        'name': '统一 BI 报表',
        'file': 'unified_report.py',
        'output': 'unified_report.html',
        'description': '9 Tab 交互式 HTML 综合报表 (营收/生命周期/产品/画像/行为/聚类/分层迁移/评价)',
    },
    {
        'name': '数据探索报告',
        'file': 'generate_exploration_report.py',
        'output': 'tmall_exploration_report.docx',
        'description': 'Word 格式数据探索分析报告 (10 章节)',
    },
    {
        'name': 'PRD 产品需求文档',
        'file': 'generate_prd.py',
        'output': 'tmall_prd.docx',
        'description': 'Word 格式产品需求文档 (含技术架构与迭代计划)',
    },
    {
        'name': '项目总结报告',
        'file': 'generate_summary.py',
        'output': 'tmall_summary.docx',
        'description': 'Word 格式项目总结报告 (含经验总结与 AI 协作复盘)',
    },
    {
        'name': '未来扩展规划',
        'file': 'generate_future_plan.py',
        'output': 'tmall_future_plan.docx',
        'description': 'Word 格式未来扩展规划报告 (技术升级 + 功能迭代路径)',
    },
]


def print_header(title):
    """打印带分隔线的标题"""
    width = 68
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def print_sub(text):
    """打印子标题"""
    print(f"\n  >>> {text}")


def check_environment():
    """检查 Python 环境和依赖包"""
    print_header("环境检查")
    print(f"  Python 版本: {sys.version}")
    print(f"  工作目录: {PROJECT_DIR}")

    required = ['pymysql', 'pandas', 'numpy', 'sklearn', 'plotly', 'docx']
    missing = []
    for pkg in required:
        try:
            __import__(pkg.replace('-', '_'))
            print(f"  [OK] {pkg}")
        except ImportError:
            print(f"  [MISSING] {pkg}")
            missing.append(pkg)

    if missing:
        print(f"\n  !!! 缺少依赖包: {', '.join(missing)}")
        print(f"  请运行: pip install {' '.join(missing)}")
        return False
    return True


def check_database():
    """检查数据库连接"""
    print_header("数据库连接检查")
    try:
        from user_auth_db import DB_CONFIG, get_connection
        # 不泄露密码
        safe_config = {**DB_CONFIG, 'password': '***'}
        print(f"  连接配置: {safe_config}")

        password = DB_CONFIG.get('password', '')
        if password in ('YOUR_PASSWORD_HERE', '', '请替换为实际密码'):
            print("  [WARNING] 数据库密码尚未配置!")
            print("  请编辑 user_auth_db.py，将 DB_CONFIG['password'] 改为实际 MySQL 密码。")
            return False

        print("  正在连接数据库...")
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DATABASE();")
        db_name = cursor.fetchone()[0]
        cursor.execute("SHOW TABLES;")
        tables = [t[0] for t in cursor.fetchall()]
        cursor.close()
        conn.close()
        print(f"  [OK] 数据库连接成功: {db_name}")
        print(f"  表列表: {', '.join(tables)}")
        return True
    except Exception as e:
        print(f"  [FAILED] 数据库连接失败: {e}")
        return False


def run_script(script_info, step_mode=False):
    """运行单个脚本，返回是否成功"""
    name = script_info['name']
    file = script_info['file']
    desc = script_info['description']

    print_header(f"运行: {name}")
    print(f"  脚本: {file}")
    print(f"  功能: {desc}")

    if step_mode:
        response = input("\n  是否运行? [Y/n]: ").strip().lower()
        if response and response != 'y':
            print("  [SKIP] 用户跳过")
            return None

    script_path = os.path.join(PROJECT_DIR, file)
    if not os.path.exists(script_path):
        print(f"  [ERROR] 脚本不存在: {script_path}")
        return False

    start = time.time()
    try:
        # 使用 importlib 而非 subprocess，确保在同一进程中运行
        import importlib.util
        spec = importlib.util.spec_from_file_location(file.replace('.py', ''), script_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[file.replace('.py', '')] = module
        spec.loader.exec_module(module)
        elapsed = time.time() - start
        print(f"\n  [OK] 完成 (耗时 {elapsed:.1f}s)")

        # 检查输出文件
        if script_info['output']:
            output_file = os.path.join(PROJECT_DIR, script_info['output'])
            if os.path.exists(output_file):
                size_kb = os.path.getsize(output_file) / 1024
                print(f"  输出文件: {script_info['output']} ({size_kb:.0f} KB)")
        return True
    except Exception as e:
        elapsed = time.time() - start
        print(f"\n  [FAILED] 运行失败 (耗时 {elapsed:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        return False


def run_all(step_mode=False):
    """运行所有脚本"""
    print_header("天猫用户销售数据分析平台")
    print(f"  启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  共 {len(SCRIPTS)} 个脚本待运行")

    # 1. 检查环境
    if not check_environment():
        print("\n环境检查未通过，请安装缺少的依赖包后重试。")
        return

    # 2. 检查数据库
    db_ok = check_database()
    if not db_ok:
        print("\n数据库连接检查未通过。")
        print("请确认:")
        print("  1. MySQL 服务已启动")
        print("  2. user_auth_db.py 中的密码已正确配置")
        print("  3. 数据库 tmall_data 已导入")
        return

    # 3. 逐个运行脚本
    results = []
    total_start = time.time()

    for i, script in enumerate(SCRIPTS):
        result = run_script(script, step_mode)
        results.append((script['name'], result))

    total_elapsed = time.time() - total_start

    # 4. 汇总报告
    print_header("运行结果汇总")
    success = 0
    failed = 0
    skipped = 0
    for name, result in results:
        if result is True:
            status = "[OK]"
            success += 1
        elif result is False:
            status = "[FAILED]"
            failed += 1
        else:
            status = "[SKIP]"
            skipped += 1
        print(f"  {status} {name}")

    print(f"\n  成功: {success} | 失败: {failed} | 跳过: {skipped}")
    print(f"  总耗时: {total_elapsed:.1f}s ({total_elapsed/60:.1f}min)")

    # 5. 列出全部输出文件
    print_header("输出文件清单")
    for script in SCRIPTS:
        if not script['output']:
            continue
        out = os.path.join(PROJECT_DIR, script['output'])
        if os.path.exists(out):
            size_kb = os.path.getsize(out) / 1024
            size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
            print(f"  {script['output']:<45s} {size_str:>10s}")
        else:
            print(f"  {script['output']:<45s} [未生成]")

    print("\n" + "=" * 68)
    print(f"  全部任务完成!  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 68)


def main():
    parser = argparse.ArgumentParser(
        description='天猫用户销售数据分析平台 — 一键运行入口',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py              # 运行全部脚本
  python main.py --step       # 逐步确认模式
  python main.py --check      # 仅检查环境和数据库
        """
    )
    parser.add_argument('--step', action='store_true', help='逐步确认模式，每个脚本运行前询问')
    parser.add_argument('--check', action='store_true', help='仅检查环境和数据库连接')

    args = parser.parse_args()

    if args.check:
        check_environment()
        check_database()
    else:
        run_all(step_mode=args.step)


if __name__ == '__main__':
    main()
