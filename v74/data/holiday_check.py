#!/usr/bin/env python3
"""
自动获取A股休市日历
从搜索引擎获取交易所公告的法定节假日，缓存到本地。
"""
import json, re, sys, time
from datetime import date, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

CACHE_FILE = Path(__file__).parent / "holiday_cache.json"

# 搜索引擎的节假日查询
SEARCH_URL = "https://www.baidu.com/s?wd="

def search_holidays(year):
    """通过网络搜索获取当年节假日"""
    queries = [
        f"{year}年A股休市安排 沪深北交易所",
        f"{year}年A股放假 交易日历",
        f"{year}年节假日安排 国务院",
    ]
    # 合并搜索结果
    all_holidays = set()
    for q in queries:
        try:
            url = SEARCH_URL + quote(q)
            req = Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
            })
            resp = urlopen(req, timeout=15)
            html = resp.read().decode('utf-8', errors='ignore')
            
            # 从搜索结果中提取YYYY年M月D日到YYYY年M月D日格式的日期范围
            # 常见的表述: "5月1日（星期五）至5月5日（星期二）休市"
            patterns = [
                r'(\d+)月(\d+)日[^至]*?至[^至]*?(\d+)月(\d+)日',
                r'(\d+)月(\d+)日至(\d+)月(\d+)日',
                r'(\d+)月(\d+)日[^休]*?休市',
            ]
            for pat in patterns:
                matches = re.findall(pat, html)
                for m in matches:
                    if len(m) == 4:
                        m1, d1, m2, d2 = int(m[0]), int(m[1]), int(m[2]), int(m[3])
                        # 生成范围内的每一天
                        from datetime import timedelta
                        d = date(year, m1, d1)
                        end = date(year, m2, d2)
                        while d <= end:
                            all_holidays.add(d.strftime("%Y%m%d"))
                            d += timedelta(days=1)
                    elif len(m) == 2:
                        m1, d1 = int(m[0]), int(m[1])
                        all_holidays.add(f"{year}{m1:02d}{d1:02d}")
        except:
            continue
        time.sleep(1)  # 避免请求过快
    
    return sorted(all_holidays)


def get_known_holidays():
    """内置已知的2026年节假日（备用）"""
    return [
        "20260101",  # 元旦
        "20260127", "20260128", "20260129", "20260130",
        "20260202", "20260203",  # 春节
        "20260406",  # 清明节
        "20260501", "20260504", "20260505",  # 劳动节
        "20260619",  # 端午节
        "20261001", "20261002", "20261005", "20261006", "20261007",  # 国庆
    ]


def update_cache():
    """更新节假日缓存"""
    year = date.today().year
    
    # 先尝试网络搜索
    holidays = search_holidays(year)
    
    # 如果搜索不到，用内置列表
    if len(holidays) < 5:
        holidays = get_known_holidays()
    
    cache = {
        "year": year,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "holidays": holidays,
    }
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    print(f"缓存已更新: {len(holidays)}个节假日")
    return holidays


def check_today():
    """检查今天是否交易日"""
    if not CACHE_FILE.exists():
        print("缓存不存在，更新中...")
        holidays = update_cache()
    else:
        cache = json.loads(CACHE_FILE.read_text())
        # 如果缓存过期（超过30天），更新
        updated = datetime.strptime(cache["updated"], "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - updated).days > 30:
            print("缓存过期，更新中...")
            holidays = update_cache()
        else:
            holidays = cache["holidays"]
    
    today = date.today()
    ds = today.strftime("%Y%m%d")
    dow = today.weekday()
    
    # 周末
    if dow >= 5:
        print(f"非交易日（周末）")
        return False
    
    # 节假日
    if ds in holidays:
        print(f"非交易日（法定节假日）")
        return False
    
    print(f"交易日")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--update":
        update_cache()
    elif len(sys.argv) > 1 and sys.argv[1] == "--list":
        holidays = get_known_holidays() if not CACHE_FILE.exists() else json.loads(CACHE_FILE.read_text())["holidays"]
        for h in holidays:
            print(h)
    else:
        is_trade = check_today()
        sys.exit(0 if is_trade else 1)
