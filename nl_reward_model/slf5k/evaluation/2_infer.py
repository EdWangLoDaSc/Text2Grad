import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from torch.utils.data import DataLoader, Dataset
import json
import random
from tqdm import tqdm
from peft import PeftModel
import re
import os
import argparse

SYSTEM_PROMPT = """
Your should evaluate the generated summary based on the original post.
  1. Please firstly give textual feedback about the quality of the summary.
  2. After that, please assign scores to each word in the generated summary based on the textual feedback.

# The Scoring Rules is:
  - -1: The word has a negative contribution to summarizing the original content.
  - 0: The word has a neutral contribution to summarizing the original content.
  - 1: The word has a positive contribution to summarizing the original content.

# Input Format
Input data is structured as JSON with the following fields:
{{
  "original_post": "The text of the original Reddit post.",
  "generated_summary": "The text of the summary generated by the model.",
}}

# Output Format
{{
  "textual_feedback":"feedback indicating missing, incorrect, or unnecessary content in the generated summary.",
  "word_score_list":
    [
    ( "word1", "Score (-1 or 0 or 1)"),
    ( "word2", "Score (-1 or 0 or 1)"),
    ...
    ]
}}

"""

def parse_args():
    parser = argparse.ArgumentParser(description="Run inference with a reward model")
    parser.add_argument("--model_path", type=str, 
                        default="../ckpt/llama31-8B/0_4800_merge",
                        help="Path to the model")
    parser.add_argument("--data_path", type=str,
                        default="../SLF5K_label/validation_critique_processed.json",
                        help="Path to the validation data")
    parser.add_argument("--output_path", type=str,
                        default="../QA_FS_EVAL/0_4800_res_span",
                        help="Path to save the results")
    parser.add_argument("--batch_size", type=int, default=24, help="Batch size for inference")
    parser.add_argument("--max_samples", type=int, default=500, help="Maximum number of samples to process")
    parser.add_argument("--max_new_tokens", type=int, default=512, help="Maximum number of new tokens to generate")
    parser.add_argument("--temperature", type=float, default=0.1, help="Temperature for sampling")
    parser.add_argument("--gpu_ids", type=str, default="0,1,2,3", help="Comma-separated list of GPU IDs to use")
    return parser.parse_args()


def load_and_prepare_model(model_path):
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True, padding_side="right")
    if not tokenizer.pad_token:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.padding_side = "right"
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=torch.float16,
        device_map="balanced",
        trust_remote_code=True
    )
    model.eval()

    return model, tokenizer


def prepare_input(post, summary):
    question = f"""# Input
{{
  "original_post": "{post}",
  "generated_summary": "{summary}"
}}

# Output
Please score each word in generated_summary based on original_post and the feedback of generated_summary, and output the responses as a JSON Dictionary without any extra information:
"""

    prompt = f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n{SYSTEM_PROMPT}<|eot_id|><|start_header_id|>user<|end_header_id|>\n{question} \n<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n"

    return prompt


def process_batch(model, tokenizer, batch_data, max_new_tokens, temperature):
    prompts = [prepare_input(data["post"], data["generated_summary"]) for data in batch_data]
    inputs = tokenizer(prompts, return_tensors="pt", padding=True, truncation=True).to(model.device)

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            do_sample=True,
            pad_token_id=tokenizer.pad_token_id
        )

    responses = []
    for i, output in enumerate(outputs):
        input_length = inputs['input_ids'].shape[1]

        response_tokens = output[input_length:]
        response = tokenizer.decode(response_tokens, skip_special_tokens=True).strip()

        print(response)

        responses.append(response)

    return responses


def extract_textual_feedback(response):
    try:
        response_dict = json.loads(response)
        return response_dict.get("textual_feedback", "")
    except json.JSONDecodeError:
        match = re.search(r'"textual_feedback"\s*:\s*"([^"]*)"', response)
        if match:
            return match.group(1)
        return ""


def extract_word_scores(response):
    try:
        response_dict = json.loads(response)
        word_score_list = response_dict.get("word_score_list", [])

        if isinstance(word_score_list, str):
            tuples = re.findall(r'\([\"\']?([^\"\',]+)[\"\']?,\s*(-?\d+)\)', word_score_list)
            return [(word, int(score)) for word, score in tuples]
        elif isinstance(word_score_list, list):
            if word_score_list and isinstance(word_score_list[0], dict):
                if "word" in word_score_list[0] and "score" in word_score_list[0]:
                    return [(item["word"], int(item["score"])) for item in word_score_list]
                elif "word" in word_score_list[0] and "Score" in word_score_list[0]:
                    return [(item["word"], int(item["Score"])) for item in word_score_list]
            elif word_score_list and isinstance(word_score_list[0], (list, tuple)):
                return [(str(item[0]), int(item[1])) for item in word_score_list]

        print(f"Warning: Using fallback extraction for format: {type(word_score_list)}")
        if isinstance(word_score_list, list):
            result = []
            for item in word_score_list:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    word = str(item[0])
                    score = item[1]
                    if isinstance(score, str):
                        try:
                            score = int(score)
                        except ValueError:
                            score_match = re.search(r'(-?\d+)', score)
                            score = int(score_match.group(1)) if score_match else 0
                    result.append((word, score))
                elif isinstance(item, dict) and len(item) >= 2:
                    word_key = next((k for k in item.keys() if k.lower() in ['word', 'token']), None)
                    score_key = next((k for k in item.keys() if k.lower() in ['score', 'value']), None)
                    if word_key and score_key:
                        word = str(item[word_key])
                        score = item[score_key]
                        if isinstance(score, str):
                            try:
                                score = int(score)
                            except ValueError:
                                score_match = re.search(r'(-?\d+)', score)
                                score = int(score_match.group(1)) if score_match else 0
                        result.append((word, score))
            return result

        return []
    except json.JSONDecodeError:
        patterns = [
            r'\([\"\']?([^\"\',]+)[\"\']?,\s*(-?\d+)\)',
            r'\[[\"\']?([^\"\',]+)[\"\']?,\s*(-?\d+)\]', 
            r'{\s*[\"\']?word[\"\']?\s*:\s*[\"\']?([^\"\',]+)[\"\']?\s*,\s*[\"\']?score[\"\']?\s*:\s*(-?\d+)\s*}'  # {"word": "word", "score": 1}
        ]

        for pattern in patterns:
            tuples = re.findall(pattern, response)
            if tuples:
                return [(word, int(score)) for word, score in tuples]

        json_match = re.search(r'({[\s\S]*})', response)
        if json_match:
            try:
                json_str = json_match.group(1)
                json_data = json.loads(json_str)
                if "word_score_list" in json_data:
                    return extract_word_scores(json_str)
            except json.JSONDecodeError:
                pass

        return []


def main():
    args = parse_args()
    
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_ids
    
    model, tokenizer = load_and_prepare_model(args.model_path)

    print("\nLoading validation data...")
    try:
        with open(args.data_path, "r") as f:
            val_data = json.load(f)
        print(f"Validation data type: {type(val_data)}")
        print(f"Total validation samples: {len(val_data)}")

        if isinstance(val_data, dict):
            first_key = next(iter(val_data))
            print("First item structure:", json.dumps({first_key: val_data[first_key]}, indent=2)[:200] + "...")
    except Exception as e:
        print(f"Error loading validation data: {str(e)}")
        return

    print("\nSampling data...")
    try:
        if isinstance(val_data, dict):
            sample_keys = random.sample(list(val_data.keys()), min(args.max_samples, len(val_data)))
            sampled_data = [val_data[key] for key in sample_keys]
        else:
            sample_indices = random.sample(range(len(val_data)), min(args.max_samples, len(val_data)))
            sampled_data = [val_data[i] for i in sample_indices]

        print(f"Sampled {len(sampled_data)} items")
    except Exception as e:
        print(f"Error sampling data: {str(e)}")
        return

    results = []

    for i in tqdm(range(0, len(sampled_data), args.batch_size)):
        batch_data = sampled_data[i:i + args.batch_size]
        responses = process_batch(model, tokenizer, batch_data, args.max_new_tokens, args.temperature)

        print("\n" + "=" * 50)
        print(f"BATCH {i // args.batch_size + 1} - FIRST PREDICTION:")
        print("-" * 50)
        print(f"Original Post: {batch_data[0]['post'][:100]}...")
        print(f"Generated Summary: {batch_data[0]['generated_summary']}")
        print(f"Model Response: {responses[0][:200]}...")

        try:
            word_scores = extract_word_scores(responses[0])
            print("\nWord Scores:")
            for word, score in word_scores[:10]:
                print(f"  • '{word}': {score}")
                print(f"  • '{word}': {score}")
            if len(word_scores) > 10:
                print(f"  ... and {len(word_scores) - 10} more words")
        except Exception as e:
            print(f"Error extracting word scores: {e}")
        print("=" * 50)

        for j, (data, response) in enumerate(zip(batch_data, responses)):
            results.append({
                "index": i + j,
                "original_post": data["post"],
                "generated_summary": data["generated_summary"],
                "model_response": response
            })

    try:
        with open(args.output_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n✅ Results saved to: {args.output_path}")
    except Exception as e:
        print(f"Error saving results: {str(e)}")
        backup_path = "./evaluation_results_backup.json"
        try:
            with open(backup_path, "w") as f:
                json.dump(results, f, indent=2)
            print(f"Results saved to backup location: {backup_path}")
        except:
            print("Failed to save results to backup location as well.")


if __name__ == "__main__":
    main()