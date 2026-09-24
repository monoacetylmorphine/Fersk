import os
import re

import pandas as pd

from datetime import datetime, timezone, timedelta
eastern8 = timezone(timedelta(hours=8))
timestamp = datetime.now(eastern8)
today = timestamp.strftime("%Y%m%d")
yesterday = (timestamp - timedelta(days=1)).strftime("%Y-%m-%d")

# 文件名前缀
file_prefix = "xbtc-sycm"

# 目标目录
downloads_dir = "/Users/fersk/Downloads/"

xbtc_sales_data = pd.read_csv("/Users/fersk/Downloads/xbtc-daily-sales-report.csv")

def record(daily:str)->pd.DataFrame:

    # 构建正则：前缀_当日日期_随机十六进制字符串.xlsx
    pattern = re.compile(
        rf"^{re.escape(daily)}_{today}_[0-9a-fA-F]+\.xlsx$"
    )

    # 在目录中查找所有匹配的文件
    matched_files = []
    for fname in os.listdir(downloads_dir):
        if pattern.match(fname):
            matched_files.append(fname)

    if not matched_files:
        print(f"在 {downloads_dir} 中未找到匹配的文件")
        return
    else:
        # 如果有多个匹配，按文件修改时间倒序，取最新的一个
        matched_files.sort(
            key=lambda f: os.path.getmtime(os.path.join(downloads_dir, f)),
            reverse=True
        )
        target_filename = matched_files[0]
        full_path = os.path.join(downloads_dir, target_filename)
        print("匹配成功:", target_filename)
        sycm = pd.read_excel(full_path)

        yesterday_idx = (xbtc_sales_data[xbtc_sales_data.loc[:,"Date"] == yesterday]).index.start

        xbtc_sales_data.fillna(value=0, inplace=True)

        xbtc_sales_data.loc[yesterday_idx, "访客数\nUV"] = sycm.loc[0, "访客数"]
        xbtc_sales_data.loc[yesterday_idx, "客单价\nATV"] = sycm.loc[0, "客单价"]
        xbtc_sales_data.loc[yesterday_idx, "买家数\nBuyer"] = sycm.loc[0, "支付买家数"]
        xbtc_sales_data.loc[yesterday_idx, "成交金额\nSold Val"] = sycm.loc[0, "支付金额"]
        xbtc_sales_data.loc[yesterday_idx, "成交订单\nSold Order"] = sycm.loc[0, "支付子订单数"]
        xbtc_sales_data.loc[yesterday_idx, "退款金额\nReturn Val"] = sycm.loc[0, "成功退款金额"]
        xbtc_sales_data.loc[yesterday_idx, "站内推广花费\nPromotion Expenses"] = sycm.loc[0, "全站推广花费"]

        xbtc_sales_data["转化率\nCR%"] = xbtc_sales_data["买家数\nBuyer"] / xbtc_sales_data["访客数\nUV"]
        xbtc_sales_data["退款率\nRefund Rate"] = xbtc_sales_data["退款金额\nReturn Val"] / xbtc_sales_data["成交金额\nSold Val"]
        xbtc_sales_data["推广费比\nCost Ratio"] = xbtc_sales_data["站内推广花费\nPromotion Expenses"] / xbtc_sales_data["成交金额\nSold Val"]
        xbtc_sales_data["净销金额\nNet Val"] = xbtc_sales_data["成交金额\nSold Val"] - xbtc_sales_data["退款金额\nReturn Val"] - xbtc_sales_data["刷单金额\nFake Val"]

        xbtc_sales_data["sortingDate"] = pd.to_datetime(xbtc_sales_data["Date"], format="%Y-%m-%d")
        xbtc_sales_data.sort_values(by="sortingDate", inplace=True)

        return xbtc_sales_data

        print(xbtc_sales_data.loc[yesterday_idx])
        xbtc_sales_data.iloc[:yesterday_idx+1, :-1].to_csv(f"/Users/fersk/Downloads/xbtc-daily-sales-report-{today}.csv", index=False, encoding="utf-8-sig")


def preview(data:pd.DataFrame)->pd.DataFrame:





if __name__=="__main__":
    record(file_prefix=file_prefix)



