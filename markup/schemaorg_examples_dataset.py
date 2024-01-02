from io import StringIO
import json
import os
from pathlib import Path
import textwrap
import click
import pandas as pd
from models.llm import ChatGPT
from rdflib import ConjunctiveGraph, URIRef
from sklearn.model_selection import train_test_split
from tqdm import tqdm
import backoff
from openai.error import APIConnectionError, ServiceUnavailableError, Timeout, RateLimitError
from utils import _html2txt, get_schema_example, get_type_definition, jsonld_search_property, md5hex, schema_simplify

from sklearn.metrics import precision_score, recall_score, f1_score


@click.group
def cli():
    pass

@cli.command()
@click.argument("outfile", type=click.Path(file_okay=True, dir_okay=False))
def create_dataset(outfile):
    g = ConjunctiveGraph()
    g.parse("schemaorg/shacl/schemaorg_datashapes.shacl")
    g.parse("schemaorg/examples/schemaorg-all-examples.ttl", format="ttl")

    query = """
    SELECT ?ref ?prop WHERE {
        ?node   a <http://www.w3.org/ns/shacl#PropertyShape> ;
                <http://www.w3.org/ns/shacl#path> ?prop .
        ?prop <http://example.org/hasExample> ?example .
        ?example    <http://example.org/json> ?jsonld ;
                    <http://example.org/pre-markup> ?ref .
    }
    """
    df = None

    def load_json(json_str):
        try: return json.dumps(json.loads(json_str))
        except: return None

    records = []
    for qres in tqdm(g.query(query)):
        ref = qres.get("ref").toPython()
        prop = qres.get("prop").toPython()
        examples = get_schema_example(prop, focus=True)
        records.append({ "ref": ref,  "prop": prop, "examples": examples })
        
    df = pd.DataFrame.from_records(records)
    df = df.explode("examples")

    df["ref"] = df["ref"].apply(lambda x: _html2txt(x, force=True))
    df["examples"] = df["examples"].apply(load_json)
    df.dropna(inplace=True)
    df.reset_index(inplace=True, drop=True)
    df.to_feather(outfile)

@cli.command()
@click.argument("infile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.option("--limit", type=click.INT)
def evaluate_prop_checker_zs(infile, limit):
    
    llm = ChatGPT(model="gpt-3.5-turbo-16k")
    test_df = pd.read_feather(infile)
    y_pred = []
    y_true = []
    for i, row in tqdm(test_df.iterrows()):
        if i == limit:
            break

        ref, prop, examples = row["ref"], row["prop"], row["examples"]
        prop_simple = schema_simplify(URIRef(prop))
        id = md5hex(ref + str(i))
        jsonld_fn = f"./.tmp/{id}.json"
        Path(jsonld_fn).parent.mkdir(parents=True, exist_ok=True)
        with open(jsonld_fn, "w") as f:
            jsonld = json.loads(examples)
            if len(jsonld) == 1 and isinstance(list(jsonld.values())[0], str):
                jsonld = { f"http://schema.org/{k}": v for k, v in jsonld.items() }
            else:
                jsonld["@context"] = "http://schema.org/"
            json.dump(jsonld, f)
        y_true.append(1) # Pos samples only
        y_pred.append(int(llm._evaluate_semantic_conformance(jsonld_fn, in_context_learning=False, breakdown=False)["pred"] >= 0.5 ))
    
    print(y_pred, y_true)

    # Calculate precision, recall, and F1 score
    precision = precision_score(y_true, y_pred)
    recall = recall_score(y_true, y_pred)
    f1 = f1_score(y_true, y_pred)

    return { "precision": precision, "recall": recall, "f1-score": f1 }

@cli.command()
@click.argument("infile", type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.argument("outfile", type=click.Path(file_okay=True, dir_okay=False))
@click.option("--explain", is_flag=True, default=False)
@click.option("--limit", type=click.INT)
@backoff.on_exception(backoff.expo, (APIConnectionError, ServiceUnavailableError, Timeout, RateLimitError))
def generate_negative_examples(infile, outfile, explain, limit):
    llm = ChatGPT(model="gpt-4")
    in_df = pd.read_feather(infile)

    out_df = None
    records = []    
    if os.path.exists(outfile):
        out_df = pd.read_feather(outfile)
        if not out_df.empty:
            records = out_df.to_records(index=False)
            records = [dict(zip(records.dtype.names,r)) for r in records]

    
    for i, row in tqdm(in_df.iterrows()):
        if out_df is not None and not out_df.empty and row["ref"] in out_df["ref"].values:
            continue
        if i == limit:
            break
        ref, prop, examples = row["ref"], row["prop"], row["examples"]
        pos_definition = get_type_definition(prop=prop, simplify=True, include_comment=True, include_expected_types=True).get(prop)
        expected_types = [repr(et) for et in pos_definition['expected_types']]

        pos_definition_verb = f"(1) the type property is {' or '.join(expected_types)}; (2) The non-type properties align with the following description: {repr(pos_definition['comment'])}"
        neg_definition_verb = f"(1) the type property is {' or '.join(expected_types)}; (2) The non-type properties does not align with the following description: {repr(pos_definition['comment'])}"
        
        prop_name = schema_simplify(URIRef(prop))
        
        prompt = textwrap.dedent(f"""
        - Given the text below:
        ```
        {ref}
        ```

        - Given the schema.org markup below for the property "{prop_name}":

        ```json
        {examples}
        ```

        The markup is a positive example, meaning {pos_definition_verb}.
        A negative example is a markup where {neg_definition_verb}.
        
        Tasks: 
        - Fill [MASK] in the following template so that the outcome is a negative example. 

            ```json
            {{
                "{prop_name}": [MASK]
            }}
            ```
        - Explain the answer.

        Constraints: 
        - the output must only contain elements mentioned explicitly/implicitly in the input text.
        - the output must conform to JSON-LD format.
        - the output must be wrapped in a separate markdown 'json' code block, i.e,  ```json.
        - The explanation must be wrapped in a separate markdown 'text' code block, i.e, ```text.
        """)

        try:

            response = llm.query(prompt, remember=False, explain=explain)
            # response = textwrap.dedent("""
            # ```json
            # {
            #     "offerCount": "Miami Heat at Philadelphia 76ers - Game 3 (Home Game 1)"
            # }
            # ```
            # ```text
            # This is a negative example because the "offerCount" should be an integer value representing the number of available offers for the product. In this case, a string value related to the event description("Miami Heat at Philadelphia 76ers - Game 3 (Home Game 1)") is provided instead of the correct integer value (1938), which violates the description of 'The number of offers for the product'.
            # ```
            # """)

            if explain: continue

            neg_examples = []

            with StringIO(response) as rio:
                lcounter = 0
                canRecord = False
                for line in rio.readlines():
                    # print("Test: ", line)
                    if line.startswith("```"):
                        if lcounter % 2 == 0:
                            canRecord = True
                            neg_examples.append("")

                        lcounter += 1
                        # print(f"canRecord: {canRecord}, lcounter: {lcounter}")
                        continue

                    if canRecord:
                        neg_examples[-1] += line

            # neg_examples.pop() # pop once to remove empty entity
            explanation = neg_examples.pop()

            for neg_example in neg_examples:
                neg_example = json.loads(neg_example)
                # neg_example = jsonld_search_property(neg_example, prop_name)
                records.append({ "ref": ref, "prop": prop, "examples": neg_example, "explain": explanation })
        
        except Exception as err:
            raise err
            break
    out_df = pd.DataFrame.from_records(records)
    out_df.to_feather(outfile)
    out_df.to_excel(f"{Path(outfile).parent}/train.xlsx", "Negatives", engine="xlsxwriter")

    # Stats
    stats = llm.get_stats_df()
    print(stats.sum())
                

def train_test_split(infile):
    df = pd.read_feather(infile)
    X_train, X_test = train_test_split(df, test_size=0.2, random_state=42)

    traindir = "data/SchemaExamples/train"

    for i, row in X_train.iterrows():
        ref, examples = row["ref"], row["examples"]
        id = md5hex(f"{ref}{i}")
        
        corpus_fn = f"{traindir}/corpus/{id}.txt"
        Path(corpus_fn).parent.mkdir(parents=True, exist_ok=True)
        with open(corpus_fn, "w") as f:
            f.write(ref)
            
        example_fn = f"{traindir}/corpus/baseline/{id}.json"
        Path(example_fn).parent.mkdir(parents=True, exist_ok=True)
        with open(example_fn, "w") as f:
            f.write(examples)

    testdir = "data/SchemaExamples/train" 
    for i, row in X_test.iterrows():
        ref, examples = row["ref"], row["examples"]
        id = md5hex(f"{ref}{i}")
        
        corpus_fn = f"{testdir}/corpus/{id}.txt"
        Path(corpus_fn).parent.mkdir(parents=True, exist_ok=True)
        with open(corpus_fn, "w") as f:
            f.write(ref)
            
        example_fn = f"{testdir}/corpus/baseline/{id}.json"
        Path(example_fn).parent.mkdir(parents=True, exist_ok=True)
        with open(example_fn, "w") as f:
            f.write(examples)

if __name__ == "__main__":
    cli()
