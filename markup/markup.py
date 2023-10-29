import glob
from hashlib import md5
import json
import os
from pathlib import Path
import click
import pandas as pd
from models.validator import ValidatorFactory
from models.llm import ModelFactoryLLM

from utils import get_archive_url, get_page_content, get_ref_attrs

@click.group
def cli():
    pass

@cli.command()
@click.argument("infile", type=click.Path(exists=False, file_okay=True, dir_okay=False))
@click.argument("outdir", type=click.Path(exists=True, file_okay=False, dir_okay=True))
def extract_content(infile, outdir):
    df = pd.read_csv(infile)
    for source in df["source"]:
        id = md5(str(source).encode()).hexdigest()
        outfile = os.path.join(outdir, f"{id}.txt")
        
        if os.path.exists(outfile):
            print(f"{outfile} already exists...")
            continue
        
        # Scrape the page content
        try:
            target_url = get_archive_url(source)
            content = get_page_content(target_url)
        
            # Write the content
            Path(outfile).parent.mkdir(parents=True, exist_ok=True)
            with open(outfile, mode="w") as ofs:
                ofs.write(content)
        except Exception as e:
            print(f"Could not scape {id}.", e)

@cli.command()
@click.argument("url", type=click.STRING)
def scrape_webpage(url):
    print(get_page_content(url))

@cli.command()
@click.argument("url", type=click.STRING)
def get_schema_properties(url):
    print(get_ref_attrs(url))
    
@cli.command()
@click.argument("path_to_jsonld", type=click.Path(exists=True, file_okay=True, dir_okay=False))
def validate_one(path_to_jsonld):
    llm_validator = ValidatorFactory.create_validator("SchemaOrgValidator")
    llm_validator.validate(path_to_jsonld)
        
@cli.command()
@click.argument("datadir", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.argument("model", type=click.Choice(["Llama2_7B", "Llama2_70B", "Llama2_13B", "ChatGPT"]), default="Llama2_7B")
@click.pass_context
def run_markup_llm(ctx: click.Context, datadir, model):
    
    llm_model = ModelFactoryLLM.create_model(model)
    
    for document in glob.glob(f"{datadir}/*.txt"):
        outfile = os.path.join(datadir, model, f"{Path(document).stem}.json")
        Path(outfile).parent.mkdir(parents=True, exist_ok=True)
        
        if os.path.exists(outfile) and os.stat(outfile).st_size > 0:
            print(f"{outfile} already exists, skipping...")
        else:
            try:
                with open(document, "r") as dfs, open(outfile, "w") as jfs:
                    page = dfs.read()
                    jsonld = llm_model.predict(page)
                    json.dump(jsonld, jfs)
            except:
                continue
        
        ctx.invoke(validate_one, path_to_jsonld=outfile)
            
            

if __name__ == "__main__":
    cli()