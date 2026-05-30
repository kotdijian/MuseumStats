import os
import json
import time
from pathlib import Path

import requests
import pandas as pd


# ============================================================
# 基本設定
# ============================================================

APP_ID = os.environ.get("ESTAT_APP_ID")

if not APP_ID:
    raise RuntimeError(
        "環境変数 ESTAT_APP_ID が設定されていません。"
        "例: export ESTAT_APP_ID='your_app_id'"
    )

BASE_URL = "https://api.e-stat.go.jp/rest/3.0/app/json"

OUT_DIR = Path("estat_output")
OUT_DIR.mkdir(exist_ok=True)

session = requests.Session()


# ============================================================
# 共通関数
# ============================================================

def estat_get(endpoint: str, params: dict) -> dict:
    """
    e-Stat API GET共通関数
    """
    url = f"{BASE_URL}/{endpoint}"

    base_params = {
        "appId": APP_ID,
        "lang": "J",
    }
    base_params.update(params)

    r = session.get(url, params=base_params, timeout=60)
    r.raise_for_status()

    data = r.json()

    # e-Stat APIの RESULT を確認
    root_key = next(iter(data.keys()))
    result = data[root_key].get("RESULT", {})

    status = str(result.get("STATUS"))
    error_msg = result.get("ERROR_MSG")

    if status != "0":
        raise RuntimeError(
            f"e-Stat API error: STATUS={status}, ERROR_MSG={error_msg}"
        )

    return data


def ensure_list(x):
    """
    dictまたはlistで返るe-Stat JSONをlistに統一する
    """
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def save_json(data: dict, path: Path):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# ============================================================
# 1. 接続確認
# ============================================================

def check_connection():
    """
    App IDとAPI接続の最小確認
    """
    data = estat_get(
        "getStatsList",
        {
            "searchWord": "社会教育調査",
            "limit": 1,
        }
    )

    print("接続確認 OK")
    save_json(data, OUT_DIR / "00_connection_check.json")


# ============================================================
# 2. 統計表検索
# ============================================================

def search_stats_tables(
    stats_code: str,
    search_word: str,
    survey_years: str | None = None,
    limit: int = 100,
) -> pd.DataFrame:
    """
    getStatsListで統計表候補を検索する

    stats_code例:
      社会教育調査: 00400004
      人口推計:     00200524
      地方財政状況調査: 00200251
    """
    params = {
        "statsCode": stats_code,
        "searchWord": search_word,
        "limit": limit,
    }

    if survey_years:
        params["surveyYears"] = survey_years

    data = estat_get("getStatsList", params)

    save_json(
        data,
        OUT_DIR / f"01_statslist_{stats_code}_{search_word}_{survey_years or 'all'}.json"
    )

    tables = (
        data.get("GET_STATS_LIST", {})
            .get("DATALIST_INF", {})
            .get("TABLE_INF")
    )

    rows = []

    for t in ensure_list(tables):
        title = t.get("TITLE")
        if isinstance(title, dict):
            title = title.get("$")

        stat_name = t.get("STAT_NAME")
        if isinstance(stat_name, dict):
            stat_name = stat_name.get("$")

        gov_org = t.get("GOV_ORG")
        if isinstance(gov_org, dict):
            gov_org = gov_org.get("$")

        rows.append({
            "statsDataId": t.get("@id"),
            "統計名": stat_name,
            "表題": title,
            "政府統計コード": t.get("STATISTICS_NAME"),
            "作成機関": gov_org,
            "調査年月": t.get("SURVEY_DATE"),
            "更新日": t.get("UPDATED_DATE"),
            "raw": json.dumps(t, ensure_ascii=False),
        })

    df = pd.DataFrame(rows)

    out_csv = OUT_DIR / f"01_statslist_{stats_code}_{search_word}_{survey_years or 'all'}.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"統計表候補を保存: {out_csv}")
    return df


# ============================================================
# 3. メタ情報取得
# ============================================================

def get_meta_info(stats_data_id: str) -> pd.DataFrame:
    """
    getMetaInfoで地域・時間・分類コードを確認する
    """
    data = estat_get(
        "getMetaInfo",
        {
            "statsDataId": stats_data_id,
        }
    )

    save_json(data, OUT_DIR / f"02_meta_{stats_data_id}.json")

    class_objs = (
        data.get("GET_META_INFO", {})
            .get("METADATA_INF", {})
            .get("CLASS_INF", {})
            .get("CLASS_OBJ")
    )

    rows = []

    for obj in ensure_list(class_objs):
        class_id = obj.get("@id")
        class_name = obj.get("@name")
        classes = obj.get("CLASS")

        for c in ensure_list(classes):
            rows.append({
                "statsDataId": stats_data_id,
                "class_id": class_id,
                "class_name": class_name,
                "code": c.get("@code"),
                "name": c.get("@name"),
                "level": c.get("@level"),
                "unit": c.get("@unit"),
            })

    df = pd.DataFrame(rows)

    out_csv = OUT_DIR / f"02_meta_{stats_data_id}.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"メタ情報を保存: {out_csv}")
    return df


# ============================================================
# 4. 実データ取得
# ============================================================

def get_stats_data(
    stats_data_id: str,
    filters: dict | None = None,
    limit: int = 100000,
) -> pd.DataFrame:
    """
    getStatsDataで実データを取得する。
    NEXT_KEYがあればページングして全件取得する。

    filters例:
      {
        "cdArea": "13000",
        "cdTime": "2021000000",
        "cdCat01": "xxx"
      }
    """
    all_values = []
    start_position = 1

    while True:
        params = {
            "statsDataId": stats_data_id,
            "metaGetFlg": "Y",
            "cntGetFlg": "N",
            "explanationGetFlg": "N",
            "annotationGetFlg": "N",
            "replaceSpChar": 2,
            "limit": limit,
            "startPosition": start_position,
        }

        if filters:
            params.update(filters)

        data = estat_get("getStatsData", params)

        save_json(
            data,
            OUT_DIR / f"03_data_{stats_data_id}_start{start_position}.json"
        )

        root = data.get("GET_STATS_DATA", {})

        stat_data = root.get("STATISTICAL_DATA", {})
        data_inf = stat_data.get("DATA_INF", {})
        values = data_inf.get("VALUE")

        all_values.extend(ensure_list(values))

        table_inf = stat_data.get("TABLE_INF", {})
        result_inf = root.get("RESULT_INF", {})

        next_key = result_inf.get("NEXT_KEY")

        print(
            f"{stats_data_id}: start={start_position}, "
            f"取得件数={len(ensure_list(values))}, NEXT_KEY={next_key}"
        )

        if not next_key:
            break

        start_position = int(next_key)
        time.sleep(0.5)

    rows = []

    for v in all_values:
        row = {
            "value": v.get("$"),
            "unit": v.get("@unit"),
            "tab": v.get("@tab"),
            "cat01": v.get("@cat01"),
            "cat02": v.get("@cat02"),
            "cat03": v.get("@cat03"),
            "cat04": v.get("@cat04"),
            "cat05": v.get("@cat05"),
            "area": v.get("@area"),
            "time": v.get("@time"),
        }

        # e-Stat表によって catXX の数が異なるため、
        # 追加属性もすべて保持しておく
        for k, val in v.items():
            if k.startswith("@") and k[1:] not in row:
                row[k[1:]] = val

        rows.append(row)

    df = pd.DataFrame(rows)

    out_csv = OUT_DIR / f"03_data_{stats_data_id}.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"実データを保存: {out_csv}")
    return df


# ============================================================
# 5. メタ情報を使ってコード名を付与
# ============================================================

def attach_meta_names(data_df: pd.DataFrame, meta_df: pd.DataFrame) -> pd.DataFrame:
    """
    getStatsDataのコード値に、getMetaInfo由来の名称を付与する。
    area, time, cat01, cat02... などを *_name として追加。
    """
    df = data_df.copy()

    for class_id in meta_df["class_id"].dropna().unique():
        if class_id not in df.columns:
            continue

        lookup = (
            meta_df[meta_df["class_id"] == class_id]
            .drop_duplicates("code")
            .set_index("code")["name"]
            .to_dict()
        )

        df[f"{class_id}_name"] = df[class_id].map(lookup)

    return df


# ============================================================
# 6. 今回必要な統計表候補をまとめて検索
# ============================================================

def search_required_tables():
    """
    今回必要な3系統の統計表候補を検索する。
    """
    search_jobs = [
        # 博物館数：社会教育調査
        {
            "name": "museum_R3",
            "stats_code": "00400004",
            "search_word": "社会教育調査 博物館",
            "survey_years": "2021",
        },
        {
            "name": "museum_H30",
            "stats_code": "00400004",
            "search_word": "社会教育調査 博物館",
            "survey_years": "2018",
        },
        {
            "name": "museum_H27",
            "stats_code": "00400004",
            "search_word": "社会教育調査 博物館",
            "survey_years": "2015",
        },
        {
            "name": "museum_H24",
            "stats_code": "00400004",
            "search_word": "社会教育調査 博物館",
            "survey_years": "2012",
        },
        {
            "name": "museum_H21",
            "stats_code": "00400004",
            "search_word": "社会教育調査 博物館",
            "survey_years": "2009",
        },
        {
            "name": "museum_H18",
            "stats_code": "00400004",
            "search_word": "社会教育調査 博物館",
            "survey_years": "2006",
        },

        # 人口：人口推計
        {
            "name": "population",
            "stats_code": "00200524",
            "search_word": "人口推計 都道府県 各年10月1日現在",
            "survey_years": None,
        },

        # 歳入額：地方財政状況調査
        {
            "name": "revenue",
            "stats_code": "00200251",
            "search_word": "地方財政状況調査 都道府県 普通会計 歳入 決算",
            "survey_years": None,
        },
    ]

    all_rows = []

    for job in search_jobs:
        print(f"\n=== search: {job['name']} ===")

        df = search_stats_tables(
            stats_code=job["stats_code"],
            search_word=job["search_word"],
            survey_years=job["survey_years"],
            limit=100,
        )

        df.insert(0, "検索区分", job["name"])
        all_rows.append(df)

    result = pd.concat(all_rows, ignore_index=True)

    out_csv = OUT_DIR / "01_all_required_table_candidates.csv"
    result.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"\n統計表候補一覧を保存: {out_csv}")
    return result


# ============================================================
# 7. 手動で選んだ statsDataId のメタ・データ取得
# ============================================================

def fetch_selected_table(stats_data_id: str, filters: dict | None = None):
    """
    候補一覧から選んだ statsDataId について、
    メタ情報と実データを取得し、名称付きCSVも保存する。
    """
    meta = get_meta_info(stats_data_id)
    data = get_stats_data(stats_data_id, filters=filters)

    named = attach_meta_names(data, meta)

    out_csv = OUT_DIR / f"04_data_named_{stats_data_id}.csv"
    named.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"名称付き実データを保存: {out_csv}")

    return meta, data, named


# ============================================================
# 実行例
# ============================================================

if __name__ == "__main__":
    # 1. 接続確認
    check_connection()

    # 2. 今回必要な統計表候補を検索
    candidates = search_required_tables()

    print("\n--- 統計表候補 上位20件 ---")
    print(
        candidates[
            ["検索区分", "statsDataId", "統計名", "表題", "調査年月", "更新日"]
        ].head(20)
    )

    print("\n次の手順:")
    print("1. estat_output/01_all_required_table_candidates.csv を開く")
    print("2. 必要な統計表の statsDataId を確認する")
    print("3. fetch_selected_table(statsDataId) でメタ情報と実データを取得する")