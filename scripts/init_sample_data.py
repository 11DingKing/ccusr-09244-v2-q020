"""初始化示例数据的兼容入口。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.seed_data import init_db, seed_data  # noqa: E402


if __name__ == "__main__":
    init_db()
    seed_data()
