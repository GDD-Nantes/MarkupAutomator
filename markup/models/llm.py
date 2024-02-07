from collections import OrderedDict
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import textwrap
from typing import Dict
import numpy as np
import pandas as pd

import openai
from openai.error import RateLimitError, ServiceUnavailableError, Timeout, APIError
from openai.embeddings_utils import get_embedding

from rdflib import ConjunctiveGraph, URIRef
import torch
from models.validator import ValidatorFactory
from utils import extract_json, logger, collect_json, extract_preds, filter_graph, get_schema_example, get_type_definition, lookup_schema_type, schema_simplify, to_jsonld

from huggingface_hub import hf_hub_download

from transformers import AutoTokenizer, AutoModelForCausalLM, AutoModel

from pyrdf2vec import RDF2VecTransformer
from pyrdf2vec.embedders import Word2Vec
from pyrdf2vec.graphs import KG, Vertex
from pyrdf2vec.walkers import RandomWalker

from nltk import bleu, meteor, nist, chrf, gleu
from nltk.translate.bleu_score import SmoothingFunction
from nltk.stem import WordNetLemmatizer
from rouge_score import rouge_scorer

from scipy.spatial.distance import cosine, jaccard
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Download NLTK data if you haven't already
import nltk
nltk.download('punkt')
nltk.download('stopwords')
nltk.download('wordnet')
nltk.download('omw-1.4')

from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize

from hugchat import hugchat
from hugchat.login import Login
from hugchat.exceptions import ChatError

from ftlangdetect import detect as lang_detect

import pycountry
import backoff

from llm_cost_estimation import count_tokens, models, estimate_cost
from llama_cpp import Llama, llama_get_embeddings

LLM_MODEL_CACHEDIR=".models"
LLM_CACHE = {}
LLM_CACHE_FILENAME = ".cache/llm_cache.json"
if os.path.exists(LLM_CACHE_FILENAME):
    with open(LLM_CACHE_FILENAME, "r") as f:
        LLM_CACHE = json.load(f)

def preprocess_text(text: str):
    lang = lang_detect(text.replace("\n", ""))["lang"]
    if pycountry.languages.get(alpha_2=lang) is not None:
        lang = pycountry.languages.get(alpha_2=lang).name.lower()
    else:
        lang = "english"
    stop_words = set(stopwords.words(lang))
    wordnet_lemmatizer = WordNetLemmatizer()
    words = word_tokenize(text.lower())
    words = [wordnet_lemmatizer.lemmatize(word) for word in words if word.isalnum()]
    words = [word for word in words if word not in stop_words]
    return " ".join(words)

class AbstractModelLLM:
    def __init__(self) -> None:
        self.__name = "LLM"
        self._conversation = []
        self._stats = []
        
    @backoff.on_exception(backoff.expo, (ServiceUnavailableError, Timeout, RateLimitError))
    def query(self, prompt, **kwargs):
        """Prompt the model and retrieve the answer. 
        The prompt will be concatenated to the chat logs before being sent to the model

        Args:
            prompt (_type_): _description_
        """
            
        model = "gpt-3.5-turbo-16k"
        prompt_str = "\n".join(prompt.values()) if isinstance(prompt, dict) else prompt
        prompt_tokens, estimated_completion_tokens = count_tokens(prompt_str, model)
        estimated_cost = estimate_cost(prompt_str, model)

        estimation = {
            "prompt_tokens": prompt_tokens,
            "estimated_completion_tokens": estimated_completion_tokens,
            "estimated_cost": estimated_cost
        }
        
        self._stats.append(estimation)

    def get_stats_df(self):
        return pd.DataFrame.from_records(self._stats)
    
    def reset(self):
        self._conversation = []
        self._stats = []
        
    def get_name(self):
        return self.__name
    
    def chain_of_thoughts(self, plan: OrderedDict, verbose=False):
        self.reset()
        chain = []
        c_plan = deepcopy(plan)
        thought = []
        while len(c_plan) > 0:
            k, v = c_plan.popitem(last=False)
            thought.append(v)
            if k.startswith("cot") or k.startswith("expert") or len(c_plan) == 0:
                chain.append("\n".join(thought))
                thought = []
        
        responses = []
        for thought in chain:
            response = self.query(thought)
            responses.append(response)
        
        return responses if verbose else responses[-1]
    
    def map_reduce_predict(self, schema_types, content, n_chunks=5, **kwargs):

        outfile = kwargs["outfile"]

        model = "gpt-3.5-turbo-16k"
        tok_count, _ = count_tokens(content, model)
        logger.info(f"There are {tok_count} tokens in the document!")

        if tok_count <= 10000:
            return self.validate(content, **kwargs)

        sents = nltk.sent_tokenize(content)
        logger.debug(f"Broken down to {len(sents)} sentences")  
        
        json_chunks = []     
            
        chunk = int(len(sents) / n_chunks)
        for i in range(n_chunks):
            lower = i*chunk
            upper = min((i+1)*chunk, len(sents))
            chunk_content = "\n".join(sents[lower:upper])
            if len(chunk_content.strip()) == 0:
                raise RuntimeError(f"Empty chunk while combining sentences [{lower}:{upper}]")

            #TODO: cache
            chunk_outfile = f"{Path(outfile).parent}/{Path(outfile).stem}_chunk{i}.chunk"
            if os.path.exists(chunk_outfile) and os.stat(chunk_outfile).st_size > 0:
                with open(chunk_outfile, "r") as f:
                    json_chunk = json.load(f)
            else:
                json_chunk = self.predict(schema_types, chunk_content, map_reduce_chunk=i, verbose=True, **kwargs)
                with open(chunk_outfile, "w") as f:
                    json.dump(json_chunk, f)

            json_chunks.append(json_chunk)
        
        # Task
        rules = [
            f"\t- The output must include 1 main entity of type {schema_types}.\n"
            f"\t- Only use properties if the information is mentioned implicitly or explicitly in the content.\n"
            f"\t- Fill properties with as much information as possible.\n"
            f"\t- In case there are many sub-entities described, when possible, the output must include them all.\n"
            f"\t- The output should only contain the JSON code.\n"    
        ]
        
        subtarget_classes = kwargs.get("subtarget_classes") or []
        if len(subtarget_classes) > 0:
            rules.insert(3, f"\t- The output must include at least 1 sub-entity of type(s) {subtarget_classes}.")
        
        rules = "\n".join(rules)

        prompt = OrderedDict({
            "context1": textwrap.dedent(f"""
             Given chunks of JSON-LD markup generated from a single text:
            """)
        })

        for i, json_chunk in enumerate(json_chunks):
            prompt.update({
                f"chunk{i}": textwrap.dedent(f"""
                Chunk {i}:
                ```json
                {json.dumps(json_chunk, ensure_ascii = False)}
                ```
                """)
            })
        
        prompt.update({
            "task": textwrap.dedent(f"""
            - Assemble into a single JSON-LD markup so that:
            {rules}
            """)
        })
        
        response = self.query(prompt, remember=False)

        jsonld = extract_json(response)
        if not isinstance(jsonld, dict):
            raise RuntimeError(f"Expecting dict, got {type(jsonld)}, content={jsonld}")
        return jsonld
        

            
    def predict(self, schema_types, content, **kwargs) -> Dict:

        remember = kwargs.get("remember", False)
        in_context_learning =  kwargs.get("in_context_learning", False)
        chain_of_thought =  kwargs.get("chain_of_thought", False)
        expert =  kwargs.get("expert", False)
        subtarget_classes = kwargs.get("subtarget_classes") or []
        map_reduce_chunk = kwargs.get("map_reduce_chunk")

        def get_type_from_content(explain=False):
            # Get the correct schema-type
            prompt = textwrap.dedent(f"""
            -------------------
            {content}
            -------------------
            Give me the schema.org types that best describes the above content.
            The answer should be under json format.
            """)

            return prompt if explain else self.query(prompt).strip()
        
        def generate_jsonld(schema_type_urls, explain=False):
            # For each of the type, make a markup
            
            prompt = OrderedDict({
                "expert": "You are an expert in the semantic web and have deep knowledge about writing schema.org markup.",
                "context1": textwrap.dedent(f"""
                - Given the content below:
                ```txt
                    {content}
                ```
                """)
            })
                        
            for i, schema_class in enumerate(set(schema_type_urls + subtarget_classes)):
                schema_attrs = get_type_definition(schema_class, simplify=True)
                prompt.update({
                    f"definition{i}": textwrap.dedent(f"""
                    - These are the properties for Type {schema_class}:
                    ```txt
                    {schema_attrs}
                    ```
                    """)
                })

            # Task
            rules = [       
                f"\t- Only use properties if the information is mentioned implicitly or explicitly in the content.\n"
                f"\t- Fill properties with as much information as possible.\n"
                f"\t- In case there are many sub-entities described, when possible, the output must include them all.\n"
                f"\t- The output should only contain the JSON code.\n"    
            ]
            
            if map_reduce_chunk is None:
                rules.insert(1, f"\t- The output must include 1 main entity of type {schema_type_urls}.\n")
                
            if len(subtarget_classes) > 0:
                rules.insert(len(rules)-1, f"\t- The output must include at least 1 sub-entity of type(s) {subtarget_classes}.")
            
            rules = "\n".join(rules)
            prompt.update({
                "task": textwrap.dedent(f"""
                - Task: generate the JSON-LD markup that matches the content.
                - Rules: 
                {rules}     
                """)
            })

            if not expert: 
                prompt.pop("expert")
            
            if in_context_learning:
                for schema_type_url in schema_type_urls:
                    examples = get_schema_example(schema_type_url)
                    for i, example in enumerate(examples):
                        prompt.update({
                            f"example{i}": textwrap.dedent(f"""
                            Example {i}:
                            ```json
                            {example}
                            ```
                            """)
                        })
            
            return prompt if explain else self.query(prompt, remember=remember)
        
        explain = kwargs.get("explain", False)
            
        if explain:
            prompt = generate_jsonld(schema_types, explain=True)
        
            model = "gpt-3.5-turbo-16k"
            prompt_tokens, estimated_completion_tokens = count_tokens(prompt, model)
            estimated_cost = estimate_cost(prompt, model)

            return {
                "prompt_tokens": prompt_tokens,
                "estimated_completion_tokens": estimated_completion_tokens,
                "estimated_cost": estimated_cost
            }

        self.reset()
        
        jsonld_string = generate_jsonld(schema_types)
        jsonld = extract_json(jsonld_string)
        if not isinstance(jsonld, dict):
            raise RuntimeError(f"Expecting dict, got {type(jsonld)}, content={jsonld}")
        return jsonld
    
    def _evaluate_coverage(self, pred, expected, **kwargs):   
        
        target_class = kwargs["target_class"]
                    
        pred_graph = ConjunctiveGraph()
        
        try: pred_graph.parse(pred)
        except UnboundLocalError:
            return {
                "class": target_class,
                "pred": None,
                "expected": None
            }
        
        expected_graph = ConjunctiveGraph()
        expected_graph.parse(expected)
        
        type_defs = set(get_type_definition(target_class, simplify=True))       
        pred_p = extract_preds(pred_graph, target_class, simplify=True) & type_defs
        expected_p = extract_preds(expected_graph, target_class, simplify=True) & type_defs
        
        pred_p_count = len(pred_p)
        expected_p_count = len(expected_p)
        
        logger.debug(f"Definition for {target_class}: {type_defs}")
        logger.debug(f"Predicates (predicted): {pred_p}")
        logger.debug(f"Predicates (expected): {expected_p}")
                
        class_count = len(type_defs)
        
        epsilon = 1e-5
        
        pred_coverage = pred_p_count / (class_count + epsilon)
        expected_coverage = expected_p_count / (class_count + epsilon)
        
        return { 
            "class": target_class,
            "pred": pred_coverage,
            "expected": expected_coverage
        }
    
    def _evaluate_graph_emb(self, pred, expected, **kwargs):
        """Calculate the semantic distance between two KGs, i.e, two markups

        Args:
            pred (_type_): _description_
            expected (_type_): _description_

        Raises:
            NotImplementedError: _description_
        """
                
        def createKG(path_to_graph):
            g = ConjunctiveGraph()
            parse_format = Path(path_to_graph).suffix.strip(".")
            parse_format = "json-ld" if parse_format == "json" else parse_format
            g.parse(path_to_graph, format=parse_format)
            
            logger.debug(g.serialize())
            
            graph = KG()
            entities = []
                        
            for s, p, o in g:
                subj = Vertex(s.n3())
                obj = Vertex(o.n3())
                predicate = Vertex(p.n3(), predicate=True, vprev=subj, vnext=obj)
                graph.add_walk(subj, predicate, obj)
                if subj.name not in entities:
                    entities.append(subj.name)
            
            return graph, entities
        
        def embed(g: ConjunctiveGraph, entities):
            # Create our transformer, setting the embedding & walking strategy.
            transformer = RDF2VecTransformer(
                Word2Vec(epochs=1000),
                walkers=[RandomWalker(4, 10, with_reverse=True, n_jobs=2)],
                # verbose=1
            )                                    
            return transformer.fit_transform(g, entities)
        
        predicted_graph, predicted_entities = None, None
        expected_graph, expected_entities = None, None
        try:
            predicted_graph, predicted_entities = createKG(pred) 
            expected_graph, expected_entities = createKG(expected)
        except:
            return {"cosine_sim": "error_invalid_kg"}
                    
        # Get our embeddings.
        predicted_embeddings, _ = embed(predicted_graph, predicted_entities)
        expected_embeddings, _ = embed(expected_graph, expected_entities)
            
        predicted_embeddings = np.mean(predicted_embeddings, axis=0)        
        expected_embeddings = np.mean(expected_embeddings, axis=0)
        cosine_distance = cosine(np.array(predicted_embeddings).flatten(), np.array(expected_embeddings).flatten())
        logger.info(f"Cosine: {1 - cosine_distance}")
        return { "cosine_sim": 1 - cosine_distance }
            
    def _evaluate_text_emb(self, pred, expected, **kwargs):
        """Mesure the semantic similarity between the prediction text, expected text and the web page.

        Args:
            pred (_type_): _description_
            expected (_type_): _description_
        """
                
        def __evaluate(reference_text, hypothesis_text):    
            # Create TF-IDF vectorizer and transform the corpora
            # vectorizer = TfidfVectorizer()
            # tfidf_matrix = vectorizer.fit_transform([reference_text, hypothesis_text])
            
            # Load pre-trained model and tokenizer
            model_name = "bert-base-multilingual-cased"  # You can use a different model here
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            model = AutoModel.from_pretrained(model_name)
            
            refs_tokens = tokenizer(reference_text, return_tensors='pt', padding=True, truncation=True)
            hyp_tokens = tokenizer(hypothesis_text, return_tensors='pt', padding=True, truncation=True)
            
            # Get the model embeddings for the input text
            with torch.no_grad():
                refs_embeddings = model(**refs_tokens).last_hidden_state.mean(dim=1).numpy()
                hyp_embeddings = model(**hyp_tokens).last_hidden_state.mean(dim=1).numpy()
                                
                # Calculate cosine similarity
                cosine_sim = cosine_similarity(refs_embeddings, hyp_embeddings)
                logger.info(f"Cosine: {cosine_sim.item()}")
                return { "cosine_sim": cosine_sim.item() }
            
        
        reference_text = open(f"{Path(expected).parent.parent}/{Path(expected).stem.split('_')[0]}.txt", "r").read()
        predicted_text = open(pred, "r").read()
        baseline_text = open(expected, "r").read()
                
        return { 
            "webpage-pred": __evaluate(reference_text, predicted_text),
            "webpage-baseline": __evaluate(reference_text, baseline_text),
            "pred-baseline": __evaluate(predicted_text, baseline_text)
        }
    
    def _evaluate_ngrams(self, pred, expected, **kwargs):
        """Compare the verbalization of predicted KG, i.e, the generated markup and the input text.

        Args:
            pred (_type_): _description_
            expected (_type_): _description_

        Raises:
            NotImplementedError: _description_
        """
                
        def __evaluate(reference_text, hypothesis_text):
      
            stop_words = set(stopwords.words('english'))
            # Tokenize the sentences
            ref_tokens = preprocess_text(reference_text).split()
            hyp_tokens = preprocess_text(hypothesis_text).split()
    
            
            # BLEU Score
            bleu_score = bleu(ref_tokens, hyp_tokens, smoothing_function=SmoothingFunction().method3)

            # NIST Score
            nist_score = nist(ref_tokens, hyp_tokens)

            # ROUGE Score
            scorer = rouge_scorer.RougeScorer(['rouge1', 'rouge2', 'rougeL'], use_stemmer=True)
            scores = scorer.score(reference_text, hypothesis_text)
            rouge_1_score = scores['rouge1'].fmeasure
            rouge_2_score = scores['rouge2'].fmeasure
            rouge_L_score = scores['rougeL'].fmeasure
            
            # GLEU
            gleu_score = gleu(ref_tokens, hyp_tokens)
            
            # CHRF
            chrf_score = chrf(ref_tokens, hyp_tokens)
            
            # METEOR
            # meteor_score = meteor(ref_tokens, hyp_tokens)

            # Print the scores
            float_prec = 4
            logger.info(f"BLEU: {round(bleu_score, float_prec)}")
            logger.info(f"NIST Score: {round(nist_score, float_prec)}")
            logger.info(f"ROUGE-1: {round(rouge_1_score, float_prec)}")
            logger.info(f"ROUGE-2: {round(rouge_2_score, float_prec)}")
            logger.info(f"ROUGE-L: {round(rouge_L_score, float_prec)}")
            logger.info(f"GLEU: {round(gleu_score, float_prec)}")
            logger.info(f"CHLF: {round(chrf_score, float_prec)}")
            # logger.info(f"METEOR: {round(meteor_score, float_prec)}")
            
            return {
                "BLEU": bleu_score, 
                "ROUGE-1": rouge_1_score, 
                "ROUGE-2": rouge_2_score, 
                "ROUGE-L": rouge_L_score, 
                "NIST": nist_score,
                "GLEU": gleu_score,
                "CHLF": chrf_score
            }
        
        reference_text = open(f"{Path(expected).parent.parent}/{Path(expected).stem.split('_')[0]}.txt", "r").read().lower()
        predicted_text = open(pred, "r").read().lower()
        baseline_text = open(expected, "r").read().lower()
        
        return { 
            "webpage-pred": __evaluate(reference_text, predicted_text),
            "webpage-baseline": __evaluate(reference_text, baseline_text),
            "pred-baseline": __evaluate(predicted_text, baseline_text)
        }
    
    def _evaluate_shacl(self, pred, expected, **kwargs):
        """Validate the generated markup against SHACL validator

        Args:
            pred (_type_): _description_
            expected (_type_): _description_
        """
        validator = ValidatorFactory.create_validator("ShaclValidator", shape_graph="schemaorg/shacl/schemaorg_datashapes_closed.shacl")
        
        pred_outfile = f"{Path(pred).parent}/{Path(pred).stem}_shacl_pred.json"
        pred_score = validator.validate(pred, outfile=pred_outfile)
        
        expected_outfile = f"{Path(pred).parent}/{Path(expected).stem}_shacl_expected.json"
        expected_score = validator.validate(expected, outfile=expected_outfile)
        
        return { 
            "pred": pred_score,
            "expected": expected_score,
        }
        
    def _evaluate_factual_consistency(self, pred, expected = None, **kwargs):
        # validator = ValidatorFactory.create_validator("FactualConsistencyValidator", retriever="BM25RetrievalModel")
        validator = ValidatorFactory.create_validator("FactualConsistencyValidator", retriever=self)

        pred_basename = kwargs.get("basename", Path(pred).stem)
        pred_outfile = f"{Path(pred).parent}/{pred_basename}_factual_pred.json"
        pred_result = validator.map_reduce_validate(pred, outfile=pred_outfile, **kwargs)
        # pred_result = validator.validate(pred, outfile=pred_outfile, **kwargs)

        if expected is None:
            return {"pred":pred_result}
        else :
            expected_basename = kwargs.get("basename", Path(expected).stem)
            expected_outfile = f"{Path(pred).parent}/{expected_basename}_factual_expected.json"
            expected_result = validator.map_reduce_validate(expected, outfile=expected_outfile, **kwargs)
            # expected_result = validator.validate(expected, outfile=expected_outfile, **kwargs)

            return {
                "pred": pred_result,
                "expected": expected_result
            }
        
    def _evaluate_semantic_conformance(self, pred, expected=None, **kwargs):
        validator = ValidatorFactory.create_validator("SemanticConformanceValidator", retriever=self)
        
        pred_basename = kwargs.get("basename", Path(pred).stem)
        pred_outfile = f"{Path(pred).parent}/{pred_basename}_semantic_pred.json"
        # pred_result = validator.map_reduce_validate(pred, outfile=pred_outfile, **kwargs)
        pred_result = validator.validate(pred, outfile=pred_outfile, **kwargs)


        if expected is None:
            return {
                "pred": pred_result,
            }
        else:
            expected_basename = kwargs.get("basename", Path(expected).stem)
            expected_outfile = f"{Path(pred).parent}/{expected_basename}_semantic_expected.json"
            # expected_result = validator.map_reduce_validate(expected, outfile=expected_outfile)
            expected_result = validator.validate(expected, outfile=expected_outfile)
            
            return {
                "pred": pred_result,
                "expected": expected_result
            }
        
    def _evaluate_sameas(self, pred, expected, **kwargs):
        validator = ValidatorFactory.create_validator("SameAsValidator", retriever=self)
        sameas = validator.validate(pred, expected_file=expected)
        
        return {
            "sameas": sameas
        }
    
    def evaluate(self, method, pred, expected=None, **kwargs):        
        pred_verbalized_fn = None
        expected_verbalized_fn = None
        
        if method in ["text-emb", "ngrams"]:
            pred_verbalized_fn = os.path.join(Path(pred).parent, Path(pred).stem + "_pred.md")
            expected_verbalized_fn = os.path.join(Path(pred).parent, Path(pred).stem + "_expected.md")
            
            if not os.path.exists(pred_verbalized_fn) or os.stat(pred_verbalized_fn).st_size == 0:
                with open(pred_verbalized_fn, "w") as ofs:
                    filtered_graph = ConjunctiveGraph()
                    filtered_graph.parse(pred)
                    filtered_graph = filter_graph(filtered_graph, pred=URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), obj=URIRef(lookup_schema_type(schema_type)))
                    verbalized = self.verbalize(filtered_graph.serialize())
                    ofs.write(verbalized)
            
            if not os.path.exists(expected_verbalized_fn) or os.stat(expected_verbalized_fn).st_size == 0:
                with open(expected_verbalized_fn, "w") as ofs:
                    filtered_graph = ConjunctiveGraph()
                    filtered_graph.parse(expected)
                    filtered_graph = filter_graph(filtered_graph, pred=URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), obj=URIRef(lookup_schema_type(schema_type)))
                    verbalized = self.verbalize(filtered_graph.serialize())
                    ofs.write(verbalized)
        
        if method == "graph-emb":
            return self._evaluate_graph_emb(pred, expected, **kwargs)
        elif method == "text-emb":            
            return self._evaluate_text_emb(pred_verbalized_fn, expected_verbalized_fn, **kwargs)
        elif method == "ngrams":
            return self._evaluate_ngrams(pred_verbalized_fn, expected_verbalized_fn, **kwargs) 
        elif method == "shacl":
            return self._evaluate_shacl(pred, expected, **kwargs) 
        elif method == "coverage":
            return self._evaluate_coverage(pred, expected, **kwargs)
        elif method == "factual":
            return self._evaluate_factual_consistency(pred, expected, **kwargs)
        elif method == "semantic":
            return self._evaluate_semantic_conformance(pred, expected, **kwargs)
        elif method == "sameas":
            return self._evaluate_sameas(pred, expected, **kwargs)
        else:
            raise NotImplementedError(f"The evaluator for {method} is not yet implemented!")
        
    def verbalize(self, jsonld):
        self.reset()
        
        prompt = f"""
        Given the schema.org markup below:
        ------------------
        {jsonld}
        ------------------
        
        Generate the corresponding Markdown document.
        The output must only use all provided information.
        The output should contain the markdown code only.
        """
        
        result = self.query(prompt).strip()
        
        if "```" in result:
            if not result.endswith("```"):
                result += "```"
            result = re.search(r"```markdown([\w\W]*)```", result).group(1)
        return result
    
class LlamaCPP(AbstractModelLLM):
    def __init__(self, **kwargs):
        super().__init__()
        model_repo = kwargs["model_repo"]
        model_file = kwargs["model_file"]
        
        model_path = hf_hub_download(repo_id=model_repo, filename=model_file, cache_dir=".models")

        self.__llm = Llama(model_path=model_path, n_ctx=16000)
        
    def query(self, prompt, **kwargs):
        
        remember = kwargs.pop("remember", False)
        explain = kwargs.pop("explain", False)
        kwargs["temperature"] = kwargs.get("temperature", 0.0)

        super().query(prompt)

        prompt = "\n".join(prompt.values()) if isinstance(prompt, dict) else prompt

        if explain:
            logger.info(self._stats[-1])
            return

        logger.debug(f">>>> Q: {prompt}")
                
        chatgpt_prompt = {"role": "user", "content": prompt}
        if remember:
            self._conversation.append(chatgpt_prompt)
            
        history = self._conversation if remember and len(self._conversation) > 0 else [chatgpt_prompt]
        
        reply = LLM_CACHE.get(prompt)
        if reply is None:                            
            chat = self.__llm.create_chat_completion(messages=history, **kwargs)
            reply = chat["choices"][0]["message"]["content"]
            logger.debug(f">>>> A: {reply}")
        else:
            logger.debug(f">>>> A (CACHED): {reply}")
        
        if remember:
            self._conversation.append({"role": "assistant", "content": reply})
        return reply


class Vicuna_7B(LlamaCPP):
    def __init__(self, **kwargs) -> None:
        super().__init__(model_path="lmsys/vicuna-7b-v1.5-16k", **kwargs)

class Mistral_7B_Instruct(LlamaCPP):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            model_repo="TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
            model_file="mistral-7b-instruct-v0.2.Q4_0.gguf",
            **kwargs
        )   

    def query(self, prompt, **kwargs):
        if isinstance(prompt, dict) and not prompt["task"].startswith("[INST]"):
            prompt["task"] = f"[INST] {prompt['task']} [/INST]"
        return super().query(prompt, **kwargs) 

class Mixtral_8x7B_Instruct(LlamaCPP):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            model_repo="TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF",
            model_file="mixtral-8x7b-instruct-v0.1.Q4_0.gguf",
            **kwargs
        )   
    
    def query(self, prompt, **kwargs):
        if isinstance(prompt, dict) and not prompt["task"].startswith("[INST]"):
            prompt["task"] = f"[INST] {prompt['task']} [/INST]"
        return super().query(prompt, **kwargs) 
            
class GPT(AbstractModelLLM):
    def __init__(self, **kwargs) -> None:
        super().__init__()
        self.__name = "GPT"
        self._model = kwargs.get("model") or "gpt-3.5-turbo-16k"
                    
        openai.api_key_path = ".openai/API.txt"
        Path(openai.api_key_path).parent.mkdir(parents=True, exist_ok=True)
        
        if not os.path.exists(openai.api_key_path):
            openai.api_key = input('YOUR_API_KEY: ')
            with open(openai.api_key_path, "w") as f:
                f.write(openai.api_key)
        else:
            with open(openai.api_key_path, "r") as f:
                openai.api_key = f.read()
                
    @backoff.on_exception(backoff.expo, (ServiceUnavailableError, Timeout, RateLimitError, APIError))
    def query(self, prompt, **kwargs):
        
        remember = kwargs.pop("remember", False)
        explain = kwargs.pop("explain", False)
        kwargs["temperature"] = kwargs.get("temperature", 0.0)

        super().query(prompt)

        if explain:
            logger.info(self._stats[-1])
            return
        
        prompt = "\n".join(prompt.values()) if isinstance(prompt, dict) else prompt

        logger.debug(f">>>> Q: {prompt}")
                
        chatgpt_prompt = {"role": "system", "content": prompt}
        if remember:
            self._conversation.append(chatgpt_prompt)
            
        history = self._conversation if remember and len(self._conversation) > 0 else [chatgpt_prompt]
        
        reply = LLM_CACHE.get(prompt)
        if reply is None:                            
            chat = openai.ChatCompletion.create(model=self._model, messages=history, **kwargs)
            reply = chat.choices[0].message.content
            logger.debug(f">>>> A: {reply}")
        else:
            logger.debug(f">>>> A (CACHED): {reply}")
        
        if remember:
            self._conversation.append({"role": "assistant", "content": reply})
        return reply

class GPT4_32k(GPT):
    def __init__(self, **kwargs) -> None:
        super().__init__(model="gpt-4-32k")

class HuggingChatLLM(AbstractModelLLM):
    def __init__(self, **kwargs) -> None:
        super().__init__()
                
        cookie_path_dir = "./.cookies_snapshot"
        sign: Login = None
        cookies = None
        
        if not os.path.exists(cookie_path_dir):
            # Log in to huggingface and grant authorization to huggingchat
            email = input("Enter your username: ")
            passwd = input("Enter your password: ")
            sign = Login(email, passwd)
            cookies = sign.login()

            # Save cookies to the local directory
            sign.saveCookiesToDir(cookie_path_dir)
        else:
            # Load cookies when you restart your program:
            email = Path(os.listdir(cookie_path_dir)[0]).stem
            logger.info(f"Logging in as {email}")
            sign = Login(email, None)
            cookies = sign.loadCookiesFromDir(cookie_path_dir) # This will detect if the JSON file exists, return cookies if it does and raise an Exception if it's not.

        # Create a ChatBot
        self._model = hugchat.ChatBot(cookies=cookies.get_dict()) 
        
        available_models = [m.name for m in self._model.get_available_llm_models()]

        model = kwargs.get("hf_model")
        if model not in available_models:
            raise ValueError(f"{model} is not one of {available_models}!")
        
        model_id = available_models.index(model)
        self._model.switch_llm(model_id)

        
    def reset(self):
        # Create a new conversation
        self._model.delete_all_conversations()
        id = self._model.new_conversation()
        self._model.change_conversation(id)
    
    @backoff.on_exception(backoff.expo, ChatError)
    def query(self, prompt, **kwargs):
        
        remember = kwargs.pop("remember", False)
        explain = kwargs.pop("explain", False)
        kwargs["temperature"] = kwargs.get("temperature", 0.0)

        super().query(prompt)

        if explain:
            logger.info(self._stats[-1])
            return

        logger.debug(f">>>> Q: {prompt}")
        # The message history is handled by huggingchat
        reply = self._model.query(prompt, **kwargs)["text"]
        logger.debug(f">>>> A: {reply}")
        return reply

class ModelFactoryLLM:
    @staticmethod
    def create_model(model_class, **kwargs) -> AbstractModelLLM:
        return globals()[model_class](**kwargs)