from collections import OrderedDict
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
from pprint import pprint
from typing import Dict, Union

from llama_cpp import Llama
from llama_cpp.llama_speculative import LlamaPromptLookupDecoding

import pandas as pd

import openai
from openai import OpenAI, RateLimitError, APITimeoutError, APIError

from rdflib import ConjunctiveGraph, URIRef
import yaml
from models.validator import ValidatorFactory
from utils import LLAMA_CPP_CONFIG, LlamaCPPEstimator, TiktokenEstimator, chunk_document, compare_graphs_on_pred, extract_json, logger, collect_json, extract_preds, filter_graph, get_schema_example, get_type_definition, lookup_schema_type, schema_simplify, to_jsonld ,scrape_webpage

from huggingface_hub import hf_hub_download

import backoff

from llm_cost_estimation import count_tokens, estimate_cost
import instructor

LLM_MODEL_CACHEDIR=".models"
LLM_CACHE = {}
LLM_CACHE_FILENAME = ".cache/llm_cache.json"
if os.path.exists(LLM_CACHE_FILENAME):
    with open(LLM_CACHE_FILENAME, "r") as f:
        LLM_CACHE = json.load(f)

class AbstractModelLLM:
    def __init__(self, **kwargs) -> None:
        self.__name = "LLM"
        self._conversation = []
        self._stats = []

    def system(self, prompt):
        # if len(self._conversation) > 0 and self._conversation[0]['role'] == 'system':
        #     self._conversation.pop(0)
        self._conversation[0] = {'role':'system','content': prompt}
        
    @backoff.on_exception(backoff.expo, (APITimeoutError, RateLimitError, APIError))
    def explain(self, prompt, **kwargs):
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

        return estimation
        
    def get_stats_df(self):
        return pd.DataFrame.from_records(self._stats)
    
    def reset(self):
        self._conversation = []
        self._stats = []
        
    def get_name(self):
        return self.__name
    
    def tokenize(self, text):
        raise NotImplementedError("")
    
    def chain_query(self, plan: OrderedDict, verbose=False):
        self.reset()
        chain = []
        c_plan = deepcopy(plan)
        thought = []
        while len(c_plan) > 0:
            k, v = c_plan.popitem(last=False)
            thought.append(v)
            if k.startswith("chain") or len(c_plan) == 0:
                chain.append("\n".join(thought))
                thought = []
            if k.startswith("cot"):
                chain[-1] = "\n".join([chain[-1], v])
                thought = []
                                        
        responses = []
        for i, thought in enumerate(chain): 
            prompt = str(thought).replace("[PREV_RES]", responses[-1]) if i > 0 else thought
            response = self.query(prompt)
            responses.append(response)
        
        return "\n".join(responses) if verbose else responses[-1]
    
    
    def map_reduce_predict(self, schema_types, content, **kwargs):

        outfile = kwargs["outfile"]

        prompt_estimate = self.predict(schema_types, "", verbose=True, explain=True, **kwargs)
        prompt_estimate_tok_count = prompt_estimate["prompt_tokens"]

        chunk_tok_count_limit = self._context_windows_length - self._max_output_length - prompt_estimate_tok_count

        content_tok_count = self._estimator.estimate_tokens(content)
        logger.info(f"There are {content_tok_count} tokens in the document!")
        logger.info(f"Maximum {chunk_tok_count_limit} tokens allowed for 1 chunk!")

        if content_tok_count <= chunk_tok_count_limit:
            return self.predict(schema_types, content, verbose=True, **kwargs)

        # Generate chunks with overlapping
        chunks = chunk_document(content, chunk_tok_count_limit, self._estimator)
        logger.info(f"Splitted into {len(chunks)} chunks!")
        markups = []
        for i, chunk in enumerate(chunks):
            chunk_outfile = f"{Path(outfile).parent}/{Path(outfile).stem}_chunk{i}.jsonld"
            if os.path.exists(chunk_outfile) and os.stat(chunk_outfile).st_size > 0:
                with open(chunk_outfile, "r") as f:
                    current_markup = json.load(f)
            else:
                current_markup = self.predict(schema_types, chunk, verbose=True, **kwargs)
                with open(chunk_outfile, "w") as f:
                    json.dump(current_markup, f)

            markups.append(current_markup)

        jsonld = {}
        for markup in markups:
            jsonld.update(markup)

        return jsonld
                    
    def predict(self, schema_types, content, **kwargs) -> Dict:

        remember = kwargs.get("remember", False)
        in_context_learning =  kwargs.get("in_context_learning", False)
        chain_of_thought =  kwargs.get("chain_of_thought", False)
        expert =  kwargs.get("expert", False)
        subtarget_classes = kwargs.get("subtarget_classes") or []
        map_reduce_chunk = kwargs.get("map_reduce_chunk")
        prompt_template_file = kwargs.get("prompt_template")
        explain = kwargs.get("explain", False)
        use_pydantic = kwargs.get("use_pydantic", False)
        max_n_example = kwargs.get("max_n_example", None)
        outfile = kwargs["outfile"]

        classes_set = list(set(schema_types + subtarget_classes))
        
        def generate_jsonld(schema_type_urls, explain=False):
            """Progressively build prompt from template

            Args:
                schema_type_urls (_type_): _description_
                explain (bool, optional): _description_. Defaults to False.

            Returns:
                _type_: _description_
            """

            with open(prompt_template_file, "r") as f:
                prompt_template = json.load(f, object_pairs_hook=OrderedDict)
                                
            classes_set = list(set(schema_type_urls + subtarget_classes))

            rules = [       
                f"\t- Only use properties if the information is mentioned implicitly or explicitly in the content.\n",
                f"\t- Use as much properties as possible.\n",
                f"\t- Fill properties with as much information as possible.\n",
                f"\t- In case there are many sub-entities described, when possible, the output must include them all.\n",
                f"\t- Output the JSON-LD markup only.\n"    
            ]
            
            if map_reduce_chunk is None:
                rules.insert(1, f"\t- The output must include 1 main entity of type {schema_type_urls}.\n")
                
            if len(subtarget_classes) > 0:
                rules.insert(len(rules)-1, f"\t- The output must include at least 1 sub-entity of type(s) {subtarget_classes}.")

            prompt = OrderedDict()
            for comp_name, comp_template in prompt_template.items():
                if comp_name == "system":
                    prompt["system"] = comp_template.replace("[RULES]", '\n'.join(rules))
                elif comp_name == "types":
                    prompt["types"] = comp_template.replace("[SCHEMA_TYPES]", f"""{{"@type": {list(schema_type_urls)}}}""")
                elif comp_name == "content":
                    prompt["content"] = comp_template.replace("[CONTENT]", content)
                elif comp_name == "property":
                    for class_idx, schema_class in enumerate(classes_set):    
                        # Get definition for ontology
                        schema_props = get_type_definition(schema_class, simplify=True)
                        prompt[f"class_{class_idx}"] = (
                            comp_template
                                .replace("[SCHEMA_CLASS]", schema_class)
                                .replace("[SCHEMA_PROPS]", str(schema_props))
                        )
                elif comp_name == "example":
                    for class_idx, schema_class in enumerate(classes_set):         
                        # Get examples for each schema type
                        examples = get_schema_example(schema_class, include_ref=True)
                        ex_count = 0
                        for ex_idx, (ex_ref, ex_markup) in enumerate(examples):

                            if max_n_example is not None and ex_count >= max_n_example:
                                break

                            prompt[f"example{ex_idx}"] = (
                                comp_template
                                    .replace("[EXAMPLE_INDEX]", str(ex_idx))
                                    .replace("[SCHEMA_TYPE]", schema_class)
                                    .replace("[EXAMPLE_CONTENT]", ex_ref)
                                    .replace("[EXAMPLE_MARKUP]", ex_markup)
                            )

                            ex_count += 1
            
            return prompt
        
        # Main
        prompt = generate_jsonld(schema_types)
  
        if explain:
            return self.query(prompt, explain=True)

        self.reset()

        search_classes = None
        if use_pydantic:
            import pydantic_schemaorg
            search_classes=[schema_simplify(URIRef(c)) for c in classes_set]
            search_classes = [
                getattr(importlib.import_module(f"{pydantic_schemaorg.__name__}"), cls)
                for cls in search_classes
            ]

        jsonld_string = self.query(prompt, search_classes=search_classes, partial=True, json_mode=True)
        jsonld = extract_json(jsonld_string)
        if not isinstance(jsonld, dict):
            raise RuntimeError(f"Expecting dict, got {type(jsonld)}, content={jsonld}")
        return jsonld
    
    def _evaluate_jaccard_multiset(self, pred, expected, **kwargs): 
        pred_graph = ConjunctiveGraph()
        pred_graph.parse(pred)
          
        expected_graph = ConjunctiveGraph()
        expected_graph.parse(expected) 

        pred_score, expected_score = compare_graphs_on_pred(pred_graph, expected_graph)
        return { 
            "pred": pred_score,
            "expected": expected_score
        }
    
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
        validator = ValidatorFactory.create_validator("FactualConsistencyValidator", retriever=self)

        pred_basename = kwargs.get("basename", Path(pred).stem)
        pred_outfile = f"{Path(pred).parent}/{pred_basename}_factual_pred.json"
        pred_result = validator.map_reduce_validate(pred, outfile=pred_outfile, **kwargs)
        # pred_result = validator.validate(pred, outfile=pred_outfile, **kwargs)

        if expected is None:
            return {"pred": pred_result}
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
            expected_result = validator.validate(expected, outfile=expected_outfile,**kwargs)
            
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
    
    def _evaluate_compression(self, pred, expected, **kwargs):
        document = kwargs["document"]
        
        with open(document, "r") as doc_fs, open(pred, "r") as pred_fs:
            document_content = doc_fs.read()
            pred_content = pred_fs.read()

            document_tok_count = self._estimator.estimate_tokens(document_content)
            pred_tok_count = self._estimator.estimate_tokens(pred_content)

            pred_doc_compression_ratio = pred_tok_count / document_tok_count

            if expected is None:
                return {
                    "pred": pred_doc_compression_ratio
                }
            
            else:
                with open(expected, "r") as expected_fs:
                    expected_content = expected_fs.read()
                    expected_tok_count = self._estimator.estimate_tokens(expected_content)
                    expected_doc_compression_ratio = expected_tok_count / document_tok_count
    
                return {
                    "pred": pred_doc_compression_ratio,
                    "expected": expected_doc_compression_ratio
                }
    
    def evaluate(self, method, pred, expected=None, **kwargs):        
        if method == "shacl":
            return self._evaluate_shacl(pred, expected, **kwargs) 
        elif method == "coverage":
            return self._evaluate_coverage(pred, expected, **kwargs)
        elif method == "factual":
            return self._evaluate_factual_consistency(pred, expected, **kwargs)
        elif method == "semantic":
            return self._evaluate_semantic_conformance(pred, expected, **kwargs)
        elif method == "sameas":
            return self._evaluate_sameas(pred, expected, **kwargs)
        elif method == "jaccardms":
            return self._evaluate_jaccard_multiset(pred, expected, **kwargs)
        elif method == "compression":
            return self._evaluate_compression(pred, expected, **kwargs)
        else:
            raise NotImplementedError(f"The evaluator for {method} is not yet implemented!")

class LlamaCPP(AbstractModelLLM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        model_repo = kwargs["model_repo"]
        model_file = kwargs["model_file"]
        
        model_path = hf_hub_download(repo_id=model_repo, filename=model_file, cache_dir=".models")

        with open(LLAMA_CPP_CONFIG) as f:
            llama_configs = yaml.safe_load(f)
            self._context_windows_length = llama_configs["n_ctx"]
            self._max_output_length = 0.0 # Infinite output by default, adjusted if needed when using create_chat_completion
            self.__llm = Llama(
                model_path=model_path, 
                draft_model=LlamaPromptLookupDecoding(num_pred_tokens=2),
                **llama_configs
            )
        
        self._estimator = LlamaCPPEstimator(self.__llm)
        
    def query(self, prompt: OrderedDict, **kwargs):
        
        explain = kwargs.pop("explain", False)
        kwargs["temperature"] = kwargs.get("temperature", 0.0)
        stream = kwargs.pop("stream", False)
        stop = kwargs.pop("stop", None)
        search_classes = kwargs.pop("search_classes", None)
        partial = kwargs.pop("partial", False)
        json_mode = kwargs.pop("json_mode", False)
        mode = instructor.Mode.JSON_SCHEMA if json_mode else instructor.Mode.TOOLS

        if stop:
            stop = [ s.lower() for s in stop ]

        estimate_cost = self.explain(prompt)

        if explain:
            return estimate_cost
        
        self._stats.append(estimate_cost)

        system_prompt = prompt.pop("system")
        user_prompt = "\n".join(prompt.values())

        if explain:
            logger.info(self._stats[-1])
            return

        logger.debug(f">>>> SYSTEM: {system_prompt}")
        logger.debug(f">>>> Q: {user_prompt}")
                
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        reply = LLM_CACHE.get(user_prompt)
        if reply is None: 
            response = None
            if search_classes:
                response_model = Union[tuple(search_classes)] 
                if len(search_classes) == 1:
                    response_model = search_classes[0]
                    stream=False     
                logger.debug(f"response_model = {response_model}")
         
                if partial:
                    response_model = instructor.Partial[response_model]

                client = instructor.patch(
                    create=self.__llm.create_chat_completion_openai_v1, mode=instructor.Mode.JSON_SCHEMA
                )

                response = client(
                    response_model=response_model, 
                    messages=messages,
                    stream=stream,
                    **kwargs
                )
            else:
                response = self.__llm.create_chat_completion(messages=messages, stream=stream, **kwargs)

            reply = None
            if stream:
                can_stop_early = False
                reply = ""
                for response_fragment in response:

                    tok_string = response_fragment["choices"][0]["delta"].get("content")
                    if tok_string is None:
                        logger.warning(f"\"content\" is not found {response_fragment}")
                        continue

                    logger.debug(f"Stream token = {tok_string}")
                    reply += tok_string

                    for stop_token in stop:
                        if str(stop_token).lower() in str(reply).lower():
                            can_stop_early = True
                            break
                    
                    if can_stop_early: break
            elif search_classes:
                reply = response
            else:
                reply = response["choices"][0]["message"]["content"]
            logger.debug(f">>>> A: {reply}")
        else:
            logger.debug(f">>>> A (CACHED): {reply}")
        
        return reply
    
    def tokenize(self, text):
        return self.__llm.tokenize(text)


class Vicuna_7B(LlamaCPP):
    def __init__(self, **kwargs) -> None:
        super().__init__(model_path="lmsys/vicuna-7b-v1.5-16k", **kwargs)

class Mistral_7B_Instruct(LlamaCPP):
    def __init__(self, **kwargs) -> None:
        quant_method = kwargs.pop("quant_method", "Q4_K_M")
        super().__init__(
            model_repo="TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
            model_file=f"mistral-7b-instruct-v0.2.{quant_method}.gguf",
            **kwargs
        )   
    
    def query(self, prompt, **kwargs):
        if isinstance(prompt, dict):
            for k, v in prompt.items():
                if (k.startswith("task") or k == "system") and not v.startswith("[INST]"):
                    prompt[k] = f"[INST] {v} [/INST]"
        return super().query(prompt, **kwargs) 

class Mixtral_8x7B_Instruct(LlamaCPP):
    def __init__(self, **kwargs) -> None:
        quant_method = kwargs.pop("quant_method", "Q4_K_M")
        super().__init__(
            model_repo="TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF",
            model_file=f"mixtral-8x7b-instruct-v0.1.{quant_method}.gguf",
            **kwargs
        )   
    
    def query(self, prompt, **kwargs):
        if isinstance(prompt, dict):
            for k, v in prompt.items():
                if (k.startswith("task") or k == "system" ) and not v.startswith("[INST]"):
                    prompt[k] = f"[INST] {v} [/INST]"
        return super().query(prompt, **kwargs) 
    
class GPT(AbstractModelLLM):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.__name = "GPT"
        self._model = kwargs.get("model")
                    
        openai.api_key_path = ".openai/API.txt"
        Path(openai.api_key_path).parent.mkdir(parents=True, exist_ok=True)
        
        if not os.path.exists(openai.api_key_path):
            openai.api_key = input('YOUR_API_KEY: ')
            with open(openai.api_key_path, "w") as f:
                f.write(openai.api_key)
        else:
            with open(openai.api_key_path, "r") as f:
                openai.api_key = f.read()
        
        self.__llm = OpenAI(api_key=openai.api_key)
        self._estimator = TiktokenEstimator()

    @backoff.on_exception(backoff.expo, (APITimeoutError, RateLimitError, APIError))
    def query(self, prompt, **kwargs):
        
        explain = kwargs.pop("explain", False)
        kwargs["temperature"] = kwargs.get("temperature", 0.0)
        stream = kwargs.pop("stream", False)
        stop = kwargs.pop("stop", None)
        search_classes = kwargs.pop("search_classes", None)
        partial = kwargs.pop("partial", False)
        json_mode = kwargs.pop("json_mode", False)
        mode = instructor.Mode.JSON if json_mode else instructor.Mode.TOOLS

        estimate_cost = self.explain(prompt)

        if explain:
            return estimate_cost
        
        self._stats.append(estimate_cost)
        
        system_prompt = prompt.pop("system")
        user_prompt = "\n".join(prompt.values()) if isinstance(prompt, dict) else prompt

        logger.debug(f">>>> SYSTEM: {system_prompt}")
        logger.debug(f">>>> Q: {user_prompt}")
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        reply = LLM_CACHE.get(user_prompt)
        if reply is None: 
            response = None
            if search_classes:
                response_model = Union[tuple(search_classes)] 
                if len(search_classes) == 1:
                    response_model = search_classes[0]
                    stream=False    

                if partial:
                    response_model = instructor.Partial[response_model]
                
                client = instructor.patch(self.__llm, mode=mode)

                response = client.chat.completions.create(
                    model=self._model,
                    response_model=response_model, 
                    messages=messages,
                    stream=stream,
                    **kwargs
                )

            else:
                response = self.__llm.chat.completions.create(model=self._model, messages=messages, **kwargs)
            reply = None
            if stream:
                can_stop_early = False
                reply = ""
                for response_fragment in response:

                    tok_string = response_fragment["choices"][0]["delta"].get("content")
                    if tok_string is None:
                        logger.warning(f"\"content\" is not found {response_fragment}")
                        continue

                    logger.debug(f"Stream token = {tok_string}")
                    reply += tok_string

                    for stop_token in stop:
                        if str(stop_token).lower() in str(reply).lower():
                            can_stop_early = True
                            break
                    
                    if can_stop_early: break
            else:
                reply = response["choices"][0]["message"]["content"]
            logger.debug(f">>>> A: {reply}")
        else:
            logger.debug(f">>>> A (CACHED): {reply}")

        return reply

class GPT_3_Turbo_16K(GPT):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model = "gpt-3.5-turbo-16k"
        self._context_windows_length = 16385
        self._max_output_length = 4096

class GPT_4_Turbo_Preview(GPT):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model = "gpt-4-0125-preview"
        self._context_windows_length = 16385 #TODO
        self._max_output_length = 4096

class ModelFactoryLLM:
    @staticmethod
    def create_model(model_class, **kwargs) -> AbstractModelLLM:
        return globals()[model_class](**kwargs)
    

    
    