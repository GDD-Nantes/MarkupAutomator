import re
from rdflib import BNode, ConjunctiveGraph, Literal, URIRef
import requests
from io import StringIO

text = requests.get("https://schema.org/version/latest/schemaorg-all-examples.txt").text
g = ConjunctiveGraph()

with StringIO(text) as s:
    is_premarkup = False
    is_microdata = False  
    is_json = False
    is_rdfa = False  
    ent_num = 0    
    
    premarkup = ""
    microdata = "" 
    rdfa = ""
    json = ""
    types = []
    
    for line in s.readlines():
        if line.startswith("TYPES:"):
            premarkup = premarkup.strip()
            microdata = microdata.strip()
            rdfa = rdfa.strip()
            json = json.strip()

            if ent_num > 0:
                bnode = BNode()
                if premarkup != "":
                    g.add((bnode, URIRef(f"http://example.org/pre-markup"), Literal(premarkup)))
                
                if microdata != "":
                    g.add((bnode, URIRef(f"http://example.org/microdata"), Literal(microdata)))
                
                if rdfa != "":
                    g.add((bnode, URIRef(f"http://example.org/rdfa"), Literal(rdfa)))
                
                if json != "":
                    g.add((bnode, URIRef(f"http://example.org/json"), Literal(json)))
                    
                for t in types:   
                    g.add((URIRef(f"http://schema.org/{t}"), URIRef(f"http://example.org/hasExample"), bnode))
                        
            premarkup = ""
            microdata = "" 
            rdfa = ""
            json = ""
            types = []
            
            types = re.sub(r"TYPES: #eg-\d+", "", line).strip()
            types = types.split(", ")
            
            ent_num += 1
            continue
    
        if line.startswith("PRE-MARKUP"):
            is_premarkup = True
            is_microdata = False  
            is_json = False
            is_rdfa = False  
            continue
        elif line.startswith("MICRODATA"):
            is_premarkup = False
            is_microdata = True  
            is_json = False
            is_rdfa = False  
            continue
        elif line.startswith("RDFA"):
            is_premarkup = False
            is_microdata = False  
            is_json = False
            is_rdfa = True  
            continue
        elif line.startswith("JSON"):
            is_premarkup = False
            is_microdata = False  
            is_json = True
            is_rdfa = False  
            continue
        else:
            if (
                re.search(r"This example is (in )?(\w|\-)+(\s+)?only", line) is not None or
                re.search(r"<!--(\s+)?(\w|\-)+ example only(\s+)?-->", line) is not None or
                re.search(r"<!-- JSONLD only example -->", line) is not None or
                re.search(r"\((\w|\-)+ only\)", line) is not None or
                re.search(r"<div>Not available</div>", line) is not None or 
                re.search(r"No (\w|\-)+ example available", line) is not None or
                line.strip() == "TODO" or 
                re.search(r"Example is (\w|\-)+ only\.", line) is not None or 
                re.search(r"^No (\w|\-)+$", line.strip()) is not None
            ):
                continue
            elif is_microdata:
                microdata += line
            elif is_premarkup:
                premarkup += line
            elif is_rdfa:
                rdfa += line
            elif is_json:
                json += line

g.serialize("schemaorg-all-examples.ttl", format="ttl")