from itertools import chain
import json
import os
import pandas as pd
from tqdm import tqdm

tqdm.pandas()

import mapply

mapply.init(
    n_workers=4,
    chunk_size=1,
    max_chunks_per_worker=8,
    progressbar=True
)


import sys
sys.path.append(os.path.join(os.getcwd(), "markup"))

from utils import get_infos, collect_json, get_type_definition

def extract_items(data):
    data = json.loads(data) 
    return list(set(chain(*collect_json(data, value_transformer=get_infos))))

def split_items(row):
    item = row["example_snippet"]
    row["prop"], row["value"], row["type"] = item.split("[TOK_Q_DELIM]")
    return row

def clean_factual_extrinsic():
    parquet = pd.read_parquet("schemaorg/examples/misc/factual-extrinsic.parquet")
    parquet["example_snippet"] = parquet["example_snippet"].apply(extract_items)
    parquet = parquet.explode("example_snippet")
    parquet = parquet.apply(split_items, axis=1)
    parquet.drop(columns=["example_snippet", "example"], inplace=True)
    parquet = parquet.reindex(columns=["ref", "prop", "value", "type", "label"])
    parquet.drop_duplicates(inplace=True)

    print("Factual Simple")
    print(parquet["label"].value_counts())

    cleaned = parquet.to_dict(orient="records")
    with open("schemaorg/examples/factual/factual-extrinsic.json", "w") as f:
        json.dump(list(cleaned), f, ensure_ascii=False, indent=2)

def clean_factual_intrinsic():
    parquet = pd.read_parquet("schemaorg/examples/misc/factual-intrinsic.parquet")
    parquet["example_snippet"] = parquet["example_snippet"].apply(extract_items)
    parquet = parquet.explode("example_snippet")
    parquet = parquet.apply(split_items, axis=1)
    parquet.drop(columns=["example_snippet", "example"], inplace=True)
    parquet = parquet.reindex(columns=["ref", "prop", "value", "type", "label"])
    parquet.drop_duplicates(inplace=True)

    print("Factual Complex")
    print(parquet["label"].value_counts())

    cleaned = parquet.to_dict(orient="records")
    with open("schemaorg/examples/factual/factual-intrinsic.json", "w") as f:
        json.dump(list(cleaned), f, ensure_ascii=False, indent=2)

def clean_compliance():

    def extract_definition(row):
        canon_prop = f"http://schema.org/{row['prop']}"
        canon_type = f"http://schema.org/{row['type']}"
        definition = get_type_definition(class_=canon_type, prop=canon_prop, include_comment=True, simplify=True)
        row["definition"] = definition.popitem()[1]["comment"]
        return row

    parquet = pd.read_parquet("schemaorg/examples/misc/compliance.parquet")
    parquet["example_snippet"] = parquet["example_snippet"].apply(extract_items)
    parquet = parquet.explode("example_snippet")
    parquet = parquet.progress_apply(split_items, axis=1)
    parquet.drop(columns=["example_snippet", "example"], inplace=True)
    parquet = parquet.reindex(columns=["ref", "prop", "value", "type", "label"])
    parquet.drop_duplicates(inplace=True)

    # parquet = parquet.progress_apply(extract_definition, axis=1)
    parquet = parquet.mapply(extract_definition, axis=1)
    parquet = parquet.reindex(columns=["prop", "definition", "value", "type", "label"])

    print("Compliance")
    print(parquet["label"].value_counts())

    cleaned = parquet.to_dict(orient="records")
    with open("schemaorg/examples/compliance/compliance.json", "w") as f:
        json.dump(list(cleaned), f, ensure_ascii=False, indent=2)

clean_factual_intrinsic()
clean_factual_extrinsic()
#clean_compliance()