from collections import Counter
import glob
from hashlib import md5
import json
import os
from pathlib import Path
from pprint import pprint
import re
import shutil
import textwrap
import click
import openai
import pandas as pd
from rdflib import BNode, ConjunctiveGraph, Literal, URIRef
from tqdm import tqdm
from models.validator import ValidatorFactory
from models.llm import ModelFactoryLLM

from utils import chunk_document, extract_json, filter_json, logger, filter_graph, get_page_content, get_schema_example, get_type_definition, html_to_rdf_extruct, jsonld_search_property, lookup_schema_type, schema_simplify, scrape_webpage, to_jsonld, transform_json

from itertools import chain, islice
import extruct

@click.group
def cli():
    pass

@cli.command()
@click.argument("jsonld", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("key", type=click.STRING)
@click.option("--value", type=click.STRING)
@click.option("--parent", is_flag=True)
def search_jsonld(jsonld, key, value, parent):
    markup = to_jsonld(jsonld, simplify=True, clean=True)
    pprint(markup)
    pprint(jsonld_search_property(markup, key, value=value, parent=parent))

@cli.command()
@click.argument("infile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
def extract_markup(infile):
    kg_extruct = html_to_rdf_extruct(infile)
    ref_markups = to_jsonld(kg_extruct, simplify=True, keep_root=True)

    for ref_markup in ref_markups.values():
        sub_markups = jsonld_search_property(ref_markup, key="@type", value=['House', 'Product'])
        print(sub_markups)

@cli.command()
@click.argument("rdf", type=click.Path(exists=True, file_okay=True, dir_okay=False))
def convert_to_jsonld(rdf):
    data = to_jsonld(rdf, simplify=True, clean=True)
    print(json.dumps(data, ensure_ascii=False))

@cli.command()
@click.argument("url", type=click.STRING)
@click.option("--focus", is_flag=True, default=False)
def get_examples(url, focus):
    print(get_schema_example(url, focus=focus))

@cli.command()
@click.argument("url", type=click.STRING)
def extract_webpage_content(url):
    if os.path.isfile(url):
        print(scrape_webpage(url))
    elif os.path.isdir(url):
        for file in glob.glob(f"{url}/*.txt"):
            id = str(Path(file).stem)
            cache_file = f".cache/{id}_raw.html"
            new_text = scrape_webpage(cache_file)
            with open(file, "w") as f:
                f.write(new_text)
                
    elif url.startswith("http"):
        print(get_page_content(url))

@cli.command()
@click.argument("infile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
def scrape_json(infile):
    with open(infile, "r") as f:
        document = f.read()
        print(extract_json(document))

from llm_cost_estimation import count_tokens, models, estimate_cost


@cli.command()
@click.argument("infile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("chunksize", type=click.INT)
def split_document(infile, chunksize):
    with open(infile, "r") as f:
        document = f.read()

        before, _ = count_tokens(document, "gpt-4")

        total = 0
        for i, chunk in enumerate(chunk_document(document, chunksize, overlap_percentage=0.3)):
            tok_count, _ = count_tokens(chunk, "gpt-4")
            total += tok_count
            print(f"Chunk {i}: {tok_count} ")

        print(before, total)


@cli.command()
@click.option("--url", type=click.STRING)
@click.option("--prop", type=click.STRING)
@click.option("--parents", is_flag=True, default=False)
@click.option("--simple", is_flag=True, default=False)
@click.option("--expected-types", is_flag=True, default=False)
@click.option("--comment", is_flag=True, default=False)
def get_schema_properties(url, prop, parents, simple, expected_types, comment):
    result = get_type_definition(class_=url, prop=prop, parents=parents, simplify=simple, include_expected_types=expected_types, include_comment=comment)
    print(result)

@cli.command()
@click.argument("infile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("logfile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("outfile", type=click.Path(exists=False, file_okay=True, dir_okay=False))
def do_filter_json_shacl(infile, logfile, outfile):
    with open(infile, "r") as in_fs, open(logfile, "r") as log_fs, open(outfile, "w") as out_fs:
        markup = json.load(in_fs)
        log = json.load(log_fs)

        for prop, msg in log["msgs"].items():
            if prop.startswith("schema1:"):
                prop = prop.replace("schema1:", "")
                markup = filter_json(markup, prop)
            elif prop.startswith("http://schema.org/"):
                prop = prop.replace("http://schema.org/", "")
                markup = filter_json(markup, "@type", value=prop)

        json.dump(markup, out_fs, ensure_ascii=False)

@cli.command()
@click.argument("infile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("logfile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("outfile", type=click.Path(exists=False, file_okay=True, dir_okay=False))
def do_filter_json_factual(infile, logfile, outfile):
    with open(infile, "r") as in_fs, open(logfile, "r") as log_fs, open(outfile, "w") as out_fs:
        markup = json.load(in_fs)
        log = json.load(log_fs)

        ran_with_map_reduce = "aggregation" in log.keys() and len(log) > 1

        info = log["aggregation"] if ran_with_map_reduce else log["chunk_0"]

        for prop, res in info.items():
            if prop in ["status", "score"]: continue
            is_res_negative = (res == False if ran_with_map_reduce else "TOKNEG" in res.get("response") )
            if is_res_negative:
                markup = filter_json(markup, prop)
                # pprint(markup)
        json.dump(markup, out_fs, ensure_ascii=False)

@cli.command()
@click.argument("infile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("outfile", type=click.Path(exists=False, file_okay=True, dir_okay=False))
@click.argument("model", type=click.STRING)
@click.option("--explain", is_flag=True, default=False)
@click.option("--target-class", type=click.STRING, multiple=True)
@click.option("--subtarget-class", type=click.STRING, multiple=True)
@click.option("--template", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.option("--max-n-example", type=click.INT, default=1)
@click.pass_context
def generate_markup_one(ctx: click.Context, infile, outfile, model, explain, target_class, subtarget_class, template, max_n_example):

    system_prompt = textwrap.dedent(f"""
    You are an expert in the semantic web and have deep knowledge about writing schema.org markup for type {target_class}.
    """)
    llm_model = ModelFactoryLLM.create_model(model, system_prompt=system_prompt)
    
    jsonld = None
    with open(infile, "r") as dfs, open(outfile, "w") as f:
        page = dfs.read()
        if explain:
            logger.info(llm_model.map_reduce_predict(target_class, page, explain=True, subtarget_classes=subtarget_class, outfile=outfile, prompt_template=template, max_n_example=max_n_example))
        else:
            jsonld = llm_model.map_reduce_predict(target_class, page, subtarget_classes=subtarget_class, outfile=outfile, prompt_template=template, max_n_example=max_n_example)
            try:
                json.dump(jsonld, f, ensure_ascii=False) 
            except:
                f.write(str(jsonld))
    
    return jsonld

@cli.command()
@click.argument("predicted", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("model", type=click.STRING)
@click.argument("metric", type=click.Choice(["shacl", "factual", "semantic", "jaccardms"]))
@click.option("--expected", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.option("--document", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.option("--outfile", type=click.Path(file_okay=True, dir_okay=False))
@click.option("--basename", type=click.STRING)
@click.option("--target-class", type=click.STRING, multiple=True)
@click.option("--force-validate", is_flag=True, default=False)
def validate_one(predicted, model, metric, expected, document, outfile, basename, target_class, force_validate):
        
    # Function to extract dictionary values and concatenate keys to column name
    def extract_and_concat(row, col_name):
        dictionary = row[col_name]
        if isinstance(dictionary, dict):
            for key, value in dictionary.items():
                new_col_name = col_name + '-' + key
                row[new_col_name] = value
        return row
    
    llm = ModelFactoryLLM.create_model(model)

    records = []
    
    if metric == "coverage":
        for tc in target_class:
            eval_result = llm.evaluate(metric, predicted, expected, document=document, basename=basename, target_class=tc, force_validate=force_validate)
            eval_result["approach"] = model
            eval_result["metric"] = metric
            records.append(eval_result)
    else:
        eval_result = llm.evaluate(metric, predicted, expected, document=document, basename=basename, force_validate=force_validate)
        eval_result["approach"] = model
        eval_result["metric"] = metric
        records.append(eval_result)
    
    result_df = pd.DataFrame.from_records(records)
    
    # Iterate through columns and apply the function
    for col in result_df.columns:
        if col not in ['metric', 'approach']:
            result_df = result_df.apply(lambda row: extract_and_concat(row, col), axis=1)
            if isinstance(result_df[col][0], dict):
                result_df.drop(col, axis=1, inplace=True)
            
    id_vars = ['metric', 'approach', 'class'] if metric == "coverage" else ['metric', 'approach']
    result_df = pd.melt(result_df, id_vars=id_vars, var_name='instance', value_name='value')
    if outfile:
        result_df.to_csv(outfile, index=False)
    return result_df

if __name__ == "__main__":
    cli()