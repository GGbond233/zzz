import copy
import random

# typing 
from typing import List, Tuple
import time
import torch
import torch.nn.functional as F

# TODO
# from transformers import LlamaTokenizer
# tokenizer=LlamaTokenizer.from_pretrained("/home/lyh/weights/hf/vicuna_v13/7B/")

TOPK = 10  # topk for sparse tree

from transformers.generation.logits_process import (
    LogitsProcessorList,
    RepetitionPenaltyLogitsProcessor,
    TemperatureLogitsWarper,
    TopKLogitsWarper,
    TopPLogitsWarper,
)


class Timer:
    def __init__(self,name):
        self.name = name
    def __enter__(self):
        torch.cuda.synchronize()
        self.start = time.perf_counter()


    def __exit__(self, exc_type, exc_value, traceback):
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - self.start
        print(f'{self.name} took {elapsed} seconds')


def prepare_logits_processor(
        temperature: float = 0.0,
        repetition_penalty: float = 0.0,
        top_p: float = 0.0,
        top_k: int = 0
) -> LogitsProcessorList:
    processor_list = LogitsProcessorList()
    if temperature > 1e-5:
        if temperature >= 1e-5 and temperature != 1.0:
            processor_list.append(TemperatureLogitsWarper(temperature))
        if repetition_penalty > 1.0:
            processor_list.append(RepetitionPenaltyLogitsProcessor(repetition_penalty))
        if 1e-8 <= top_p < 1.0:
            processor_list.append(TopPLogitsWarper(top_p))
        if top_k > 0:
            processor_list.append(TopKLogitsWarper(top_k))
    return processor_list


# test_processor = prepare_logits_processor(
#         0.0, 0.0, -1, 1
#     )


def pad_path(path: List[int], length: int, pad_value: int = -2) -> List[int]:
    """
    Pad the given path list with a specific value up to a specified length.

    Parameters:
    - path (list): The original list that needs padding.
    - length (int): The desired length of the padded list.
    - pad_value (optional, default=-2): The value to use for padding.

    Returns:
    - list: A new list based on the original path but padded to the desired length.

    Example:
    >>> pad_path([1,2,3], 5)
    [1, 2, 3, -2, -2]

    Note:
    If the given path is already longer than the specified length,
    then no padding occurs, and the original path is returned.
    """

    # Calculate the number of padding values needed by subtracting the length
    # of the path from the desired length.
    # Append the padding values to the original path and return the new list.
    return path + [pad_value] * (length - len(path))


def generate_tree_buffers(tree_choices, device="cuda"):
    def custom_sort(lst):
        # sort_keys=[len(list)]
        sort_keys = []
        for i in range(len(lst)):
            sort_keys.append(lst[i] if lst[i] >= 0 else maxitem)
        return sort_keys
    with Timer("sort"):

        sorted_tree_choices = sorted(tree_choices, key=lambda x: (len(x), x))
        tree_len = len(sorted_tree_choices) + 1

    # Initialize depth_counts to keep track of how many choices have a particular depth
        depth_counts = []
        prev_depth = 0
        for path in sorted_tree_choices:
            depth = len(path)
            if depth != prev_depth:
                depth_counts.append(0)
            depth_counts[depth - 1] += 1
            prev_depth = depth

        tree_attn_mask = torch.eye(tree_len, tree_len)
        tree_attn_mask[:, 0] = 1
        start = 0
        for i in range(len(depth_counts)):
            for j in range(depth_counts[i]):
                cur_tree_choice = sorted_tree_choices[start + j]
                # retrieve ancestor position
                if len(cur_tree_choice) == 1:
                    continue
                ancestor_idx = []
                for c in range(len(cur_tree_choice) - 1):
                    ancestor_idx.append(sorted_tree_choices.index(cur_tree_choice[:c + 1]) + 1)
                tree_attn_mask[j + start + 1, ancestor_idx] = 1
            start += depth_counts[i]

        tree_indices = torch.zeros(tree_len, dtype=torch.long)
        p_indices = [0 for _ in range(tree_len - 1)]
        b_indices = [[] for _ in range(tree_len - 1)]
        tree_indices[0] = 0
        start = 0
        bias = 0
        for i in range(len(depth_counts)):
            inlayer_bias = 0
            b = []
            for j in range(depth_counts[i]):
                cur_tree_choice = sorted_tree_choices[start + j]
                cur_parent = cur_tree_choice[:-1]
                if j != 0:
                    if cur_parent != parent:
                        bias += 1
                        inlayer_bias += 1
                        parent = cur_parent
                        b = []
                else:
                    parent = cur_parent
                tree_indices[start + j + 1] = cur_tree_choice[-1] + TOPK * (i + bias) + 1
                p_indices[start + j] = inlayer_bias
                if len(b) > 0:
                    b_indices[start + j] = copy.deepcopy(b)
                else:
                    b_indices[start + j] = []
                b.append(cur_tree_choice[-1] + TOPK * (i + bias) + 1)
            start += depth_counts[i]

        p_indices = [-1] + p_indices
        tree_position_ids = torch.zeros(tree_len, dtype=torch.long)
        start = 0
        for i in range(len(depth_counts)):
            tree_position_ids[start + 1: start + depth_counts[i] + 1] = i + 1
            start += depth_counts[i]

        retrieve_indices_nest = []
        retrieve_paths = []
        for i in range(len(sorted_tree_choices)):
            cur_tree_choice = sorted_tree_choices[-i - 1]
            retrieve_indice = []
            if cur_tree_choice in retrieve_paths:
                continue
            else:
                for c in range(len(cur_tree_choice)):
                    retrieve_indice.append(sorted_tree_choices.index(cur_tree_choice[:c + 1]))
                    retrieve_paths.append(cur_tree_choice[:c + 1])
            retrieve_indices_nest.append(retrieve_indice)
        max_length = max([len(x) for x in retrieve_indices_nest])
        retrieve_indices = [pad_path(path, max_length) for path in retrieve_indices_nest]
        retrieve_indices = torch.tensor(retrieve_indices, dtype=torch.long)
        retrieve_indices = retrieve_indices + 1
        retrieve_indices = torch.cat([torch.zeros((retrieve_indices.shape[0], 1), dtype=torch.long), retrieve_indices],
                                     dim=1)

        maxitem = retrieve_indices.max().item() + 5



        retrieve_indices = retrieve_indices.tolist()
        retrieve_indices = sorted(retrieve_indices, key=custom_sort)
        retrieve_indices = torch.tensor(retrieve_indices, dtype=torch.long)



    # Aggregate the generated buffers into a dictionary
    tree_buffers = {
        "tree_attn_mask": tree_attn_mask.unsqueeze(0).unsqueeze(0),
        "tree_indices": tree_indices,
        "tree_position_ids": tree_position_ids,
        "retrieve_indices": retrieve_indices,
    }

    # Move the tensors in the dictionary to the specified device
    tree_buffers = {
        k: v.clone().to(device)
        if isinstance(v, torch.Tensor)
        else torch.tensor(v, device=device)
        for k, v in tree_buffers.items()
    }

    return tree_buffers


def initialize_tree0(input_ids, model, past_key_values, logits_processor):
    draft_tokens, retrieve_indices,tree_mask,tree_position_ids, outputs, logits, hidden_state, sample_token = model(
        input_ids, past_key_values=past_key_values, output_orig=True, logits_processor=logits_processor
    )

    return draft_tokens, retrieve_indices,tree_mask,tree_position_ids, logits, hidden_state, sample_token

def initialize_tree(input_ids, model, past_key_values, logits_processor):
    # 检查是否使用MOE模型
    if hasattr(model, 'use_moe') and model.use_moe:
        return initialize_tree_with_moe(input_ids, model, past_key_values, logits_processor)
    else:
        # 原始EAGLE模型的实现
        outputs, orig, hidden_states = model(
            input_ids, past_key_values=past_key_values, output_orig=True
        )

        if logits_processor is not None:
            logits = orig[:, -1]
            logits = logits_processor(None, logits)
            probabilities = torch.nn.functional.softmax(logits, dim=1)
            token = torch.multinomial(probabilities, 1)
        else:
            token = torch.argmax(orig[:, -1])
            token = token[None, None]
        input_ids = torch.cat((input_ids, token.to(input_ids.device)), dim=1)
        # Clone the output hidden states

        draft_tokens, retrieve_indices, tree_mask, tree_position_ids = model.ea_layer.topK_genrate(
            hidden_states, input_ids, model.base_model.lm_head, logits_processor
        )
        return draft_tokens, retrieve_indices, tree_mask, tree_position_ids, orig, hidden_states, token

def initialize_tree_with_moe(input_ids, model, past_key_values, logits_processor):
    """
    使用MOE模型初始化树状草稿
    """
    # 获取输入的隐藏状态
    outputs, orig, hidden_states = model(
        input_ids, past_key_values=past_key_values, output_orig=True
    )
    
    # 使用LM头来预测下一个token
    if logits_processor is not None:
        logits = orig[:, -1]
        logits = logits_processor(None, logits)
        probabilities = torch.nn.functional.softmax(logits, dim=1)
        token = torch.multinomial(probabilities, 1)
    else:
        token = torch.argmax(orig[:, -1])
        token = token[None, None]
    
    # 将预测的token添加到输入
    input_ids = torch.cat((input_ids, token.to(input_ids.device)), dim=1)
    
    # 使用MOE模型生成树状草稿
    batch_size = input_ids.shape[0]
    device = input_ids.device
    
    # 确定草稿生成的总tokens数量
    total_tokens = getattr(model.ea_layer, 'total_tokens', 59)
    
    # 创建用于存储草稿tokens的张量
    draft_tokens = torch.zeros((batch_size, total_tokens), dtype=torch.long, device=device)
    
    # 使用MOE模型递归生成树状草稿
    # 初始输入为当前隐藏状态和输入tokens
    curr_hidden = hidden_states
    curr_tokens = input_ids
    
    # 获取token嵌入
    token_embeddings = model.ea_layer.embedding(curr_tokens)
    
    # 创建树状结构
    tree_choices = []
    current_path = []
    
    # 生成深度为depth的树
    depth = model.ea_layer.depth if hasattr(model.ea_layer, 'depth') else 5
    top_k = model.ea_layer.top_k if hasattr(model.ea_layer, 'top_k') else 10
    
    # 递归生成树
    def generate_tree(path, d, parent_hidden, parent_tokens):
        if d >= depth:
            return
        
        # 使用MOE模型预测下一个特征和logits
        next_features, logits = model.ea_layer(parent_hidden[:, -1:], parent_tokens[:, -1:])
        logits = logits[:, -1, :]
        
        # 获取top-k预测
        if logits_processor is not None:
            logits = logits_processor(None, logits)
        
        values, indices = torch.topk(logits, top_k, dim=-1)
        
        for i in range(top_k):
            next_token = indices[0, i].unsqueeze(0).unsqueeze(0)
            new_path = path + [indices[0, i].item()]
            tree_choices.append(new_path)
            
            # 将token嵌入转换为特征
            next_token_embed = model.ea_layer.embedding(next_token)
            next_hidden = torch.cat([parent_hidden, next_features], dim=1)
            next_tokens = torch.cat([parent_tokens, next_token], dim=1)
            
            # 递归生成下一层
            generate_tree(new_path, d + 1, next_hidden, next_tokens)
    
    # 从根节点开始生成树
    generate_tree(current_path, 0, curr_hidden, curr_tokens)
    
    # 生成树状缓冲区
    tree_buffers = generate_tree_buffers(tree_choices, device=device)
    
    # 提取必要的信息
    tree_mask = tree_buffers["tree_attn_mask"]
    tree_position_ids = tree_buffers["tree_position_ids"]
    retrieve_indices = tree_buffers["retrieve_indices"]
    
    # 使用树状结构填充draft_tokens
    for i, choice in enumerate(tree_choices):
        if i < draft_tokens.shape[1]:
            draft_tokens[0, i] = choice[-1]
    
    return draft_tokens, retrieve_indices, tree_mask, tree_position_ids, orig, hidden_states, token


def reset_tree_mode(
        model,
):
    model.base_model.model.tree_mask = None
    model.base_model.model.tree_mode = None


def reset_past_key_values(passed_key_values: List[torch.Tensor]) -> List[torch.Tensor]:
    """
    Resets the current lengths in the passed key-values to zero.

    This function is designed to be used during the evaluation of a baseline model.
    It iterates through each layer's key-values and sets their current lengths to zero,
    effectively resetting their state.

    Args:
    - passed_key_values (list of torch.Tensor): Contains past hidden states and past attention values for each layer.

    Returns:
    - passed_key_values (list of torch.Tensor): Updated past hidden states and past attention values with reset lengths.
    """
    for i in range(len(passed_key_values)):
        for j in range(2):
            passed_key_values[i][j].current_length.fill_(0)
    return passed_key_values


def generate_candidates(tree_logits, tree_indices, retrieve_indices, sample_token, logits_processor):
    sample_token = sample_token.to(tree_indices.device)

    candidates_logit = sample_token[0]

    candidates_tree_logits = tree_logits

    candidates = torch.cat([candidates_logit, candidates_tree_logits.view(-1)], dim=-1)

    tree_candidates = candidates[tree_indices]

    tree_candidates_ext = torch.cat(
        [tree_candidates, torch.zeros((1), dtype=torch.long, device=tree_candidates.device) - 1], dim=0)

    cart_candidates = tree_candidates_ext[retrieve_indices]


    # Unsqueeze the tree candidates for dimension consistency.
    tree_candidates = tree_candidates.unsqueeze(0)
    return cart_candidates,  tree_candidates


def tree_decoding(
        model,
        tree_candidates,
        past_key_values,
        tree_position_ids,
        input_ids,
        retrieve_indices,
):
    # 检查是否使用MOE模型
    if hasattr(model, 'use_moe') and model.use_moe:
        return tree_decoding_with_moe(model, tree_candidates, past_key_values, tree_position_ids, input_ids, retrieve_indices)
    else:
        # 原始EAGLE模型的树解码
        position_ids = tree_position_ids + input_ids.shape[1]

        outputs, tree_logits, hidden_state = model(
            tree_candidates,
            output_orig=True,
            past_key_values=past_key_values,
            position_ids=position_ids,
        )

        logits = tree_logits[0, retrieve_indices]
        return logits, hidden_state, outputs

def tree_decoding_with_moe(
        model,
        tree_candidates,
        past_key_values,
        tree_position_ids,
        input_ids,
        retrieve_indices,
):
    """
    使用MOE模型进行树解码
    """
    # 计算位置ID
    position_ids = tree_position_ids + input_ids.shape[1]
    
    # 前向传播获取隐藏状态
    outputs, hidden_states = model(
        tree_candidates,
        past_key_values=past_key_values,
        position_ids=position_ids,
    )
    
    # 使用MOE模型生成logits
    token_embeddings = model.ea_layer.embedding(tree_candidates)
    _, logits = model.ea_layer(hidden_states, tree_candidates)
    
    # 提取对应检索索引的logits
    retrieved_logits = logits[:, retrieve_indices]
    
    return retrieved_logits, hidden_states, outputs


def evaluate_posterior(
        logits: torch.Tensor,
        candidates: torch.Tensor,
        logits_processor,
):
    """
    Evaluate the posterior probabilities of the candidates based on the provided logits and choose the best candidate.

    Depending on the temperature value, the function either uses greedy decoding or evaluates posterior
    probabilities to select the best candidate.

    Args:
    - logits (torch.Tensor): Predicted logits of shape (batch_size, sequence_length, vocab_size).
    - candidates (torch.Tensor): Candidate token sequences.
    - temperature (float): Softmax temperature for probability scaling. A value of 0 indicates greedy decoding.
    - posterior_threshold (float): Threshold for posterior probability.
    - posterior_alpha (float): Scaling factor for the threshold.

    Returns:
    - best_candidate (torch.Tensor): Index of the chosen best candidate.
    - accept_length (int): Length of the accepted candidate sequence.
    """
    # Greedy decoding based on temperature value
    if logits_processor is None:
        # Find the tokens that match the maximum logits for each position in the sequence
        posterior_mask = (
                candidates[:, 1:].to(logits.device) == torch.argmax(logits[:, :-1], dim=-1)
        ).int()
        candidates_accept_length = (torch.cumprod(posterior_mask, dim=1)).sum(dim=1)
        accept_length = candidates_accept_length.max()
        # Choose the best candidate
        if accept_length == 0:
            # Default to the first candidate if none are accepted
            best_candidate = torch.tensor(0, dtype=torch.long, device=candidates.device)
        else:
            best_candidate = torch.argmax(candidates_accept_length).to(torch.long)
        return best_candidate, accept_length, logits[best_candidate, accept_length]

    else:
        accept_length = 1
        accept_cand = candidates[0][:1]
        best_candidate = 0
        for i in range(1, candidates.shape[1]):
            if i != accept_length:
                break
            adjustflag = False
            is_eq = (candidates[:, :accept_length] == accept_cand).all(dim=1)
            fi = torch.nonzero(is_eq, as_tuple=True)[0][0]
            gt_logits = logits[fi, i - 1][None]
            gt_logits = logits_processor(None, gt_logits)[0]
            gtp = torch.softmax(gt_logits, dim=0)
            candidates_set = []
            for j in range(candidates.shape[0]):
                if is_eq[j]:
                    x = candidates[j, i]
                    xi = x.item()
                    if xi in candidates_set or xi == -1:
                        continue
                    candidates_set.append(xi)
                    r = random.random()
                    px = gtp[xi]
                    qx = 1.0
                    acp = px / qx
                    if r <= acp:
                        accept_cand = torch.cat((accept_cand, x[None]), dim=0)
                        accept_length += 1
                        best_candidate = j
                        break
                    else:
                        gtp[xi] = 0
                        gtp = gtp / gtp.sum()
                        adjustflag = True
        if adjustflag and accept_length != candidates.shape[1]:
            sample_p = gtp
        else:
            gt_logits = logits[best_candidate, accept_length - 1]
            sample_p = torch.softmax(gt_logits, dim=0)
        return torch.tensor(best_candidate), accept_length - 1, sample_p


@torch.no_grad()
def update_inference_inputs(
        input_ids,
        candidates,
        best_candidate,
        accept_length,
        retrieve_indices,
        logits_processor,
        new_token,
        past_key_values_data_list,
        current_length_data,
        model,
        hidden_state_new,
        sample_p
):
    # 检查是否使用MOE模型
    if hasattr(model, 'use_moe') and model.use_moe:
        return update_inference_inputs_with_moe(
            input_ids, candidates, best_candidate, accept_length, retrieve_indices,
            logits_processor, new_token, past_key_values_data_list, current_length_data,
            model, hidden_state_new, sample_p
        )
    else:
        # 原始EAGLE模型的实现
        prev_input_len = input_ids.shape[1]
        # Map the best candidate indices to the original indices in the sequence
        select_indices = (
                retrieve_indices[best_candidate, : accept_length + 1] + prev_input_len
        )
        # Append the tokens from the best candidate to the input sequence
        input_ids = torch.cat(
            [input_ids, candidates[None, best_candidate, : accept_length + 1].to(input_ids.device)], dim=-1
        )
        # Update the past key values based on the selected tokens
        # Source tensor that contains relevant past information based on the selected candidate
        for past_key_values_data in past_key_values_data_list:
            tgt = past_key_values_data[..., select_indices.to(past_key_values_data.device), :]
            # Destination tensor where the relevant past information will be stored
            dst = past_key_values_data[..., prev_input_len: prev_input_len + tgt.shape[-2], :]
            # Copy relevant past information from the source to the destination
            dst.copy_(tgt, non_blocking=True)

        # Update the current length tensor (currently only support batch size is 1)
        current_length_data.fill_(prev_input_len + tgt.shape[-2])

        retrieve_hidden_state_new = hidden_state_new[:, retrieve_indices]
        accept_hidden_state_new = retrieve_hidden_state_new[:, best_candidate, : accept_length + 1]
        # token=model.base_model.lm_head(accept_hidden_state_new[:,-1]).argmax()
        # token=token[None,None]
        prob = sample_p
        if logits_processor is not None:
            token = torch.multinomial(prob, 1)
            token = token[None]
        else:
            token = torch.argmax(prob)
            token = token[None, None]
        # hidden_state = torch.cat((hidden_state, accept_hidden_state_new), dim=1)
        draft_tokens, retrieve_indices,tree_mask,tree_position_ids = model.ea_layer.topK_genrate(accept_hidden_state_new,
                                                input_ids=torch.cat((input_ids, token.to(input_ids.device)), dim=1),
                                                head=model.base_model.lm_head,logits_processor=logits_processor)


        new_token += accept_length + 1

        return input_ids, draft_tokens, retrieve_indices,tree_mask,tree_position_ids, new_token, None, token

def update_inference_inputs_with_moe(
        input_ids,
        candidates,
        best_candidate,
        accept_length,
        retrieve_indices,
        logits_processor,
        new_token,
        past_key_values_data_list,
        current_length_data,
        model,
        hidden_state_new,
        sample_p
):
    """
    使用MOE模型更新推理输入
    """
    prev_input_len = input_ids.shape[1]
    
    # 映射最佳候选索引到序列中的原始索引
    select_indices = (
            retrieve_indices[best_candidate, : accept_length + 1] + prev_input_len
    )
    
    # 将最佳候选的tokens添加到输入序列
    input_ids = torch.cat(
        [input_ids, candidates[None, best_candidate, : accept_length + 1].to(input_ids.device)], dim=-1
    )
    
    # 更新过去的键值状态
    for past_key_values_data in past_key_values_data_list:
        tgt = past_key_values_data[..., select_indices.to(past_key_values_data.device), :]
        dst = past_key_values_data[..., prev_input_len: prev_input_len + tgt.shape[-2], :]
        dst.copy_(tgt, non_blocking=True)

    # 更新当前长度张量
    current_length_data.fill_(prev_input_len + tgt.shape[-2])
    
    # 获取接受的隐藏状态
    retrieve_hidden_state_new = hidden_state_new[:, retrieve_indices]
    accept_hidden_state_new = retrieve_hidden_state_new[:, best_candidate, : accept_length + 1]
    
    # 采样下一个token
    prob = sample_p
    if logits_processor is not None:
        token = torch.multinomial(prob, 1)
        token = token[None]
    else:
        token = torch.argmax(prob)
        token = token[None, None]
    
    # 生成新的树状草稿
    next_input_ids = torch.cat((input_ids, token.to(input_ids.device)), dim=1)
    
    # 获取token嵌入
    token_embeddings = model.ea_layer.embedding(next_input_ids)
    
    # 使用MOE模型生成下一批树状草稿
    # 这部分仍然需要原始的树生成函数
    if hasattr(model.ea_layer, 'topK_genrate'):
        # 如果MOE模型有topK_generate方法，使用它
        draft_tokens, retrieve_indices, tree_mask, tree_position_ids = model.ea_layer.topK_genrate(
            accept_hidden_state_new,
            input_ids=next_input_ids,
            head=model.base_model.lm_head,
            logits_processor=logits_processor
        )
    else:
        # 否则使用initialize_tree_with_moe重新生成树
        # 需要截断当前的隐藏状态并添加最新的
        _, retrieve_indices, tree_mask, tree_position_ids, _, _, _ = initialize_tree_with_moe(
            next_input_ids, model, None, logits_processor
        )
        
        # 获取草稿tokens
        batch_size = input_ids.shape[0]
        device = input_ids.device
        total_tokens = getattr(model.ea_layer, 'total_tokens', 59)
        draft_tokens = torch.zeros((batch_size, total_tokens), dtype=torch.long, device=device)
    
    new_token += accept_length + 1
    
    return input_ids, draft_tokens, retrieve_indices, tree_mask, tree_position_ids, new_token, None, token


if __name__ == "__main__":
    logits = torch.randn(1, 5)
    tp = prepare_logits_processor(0.9, 0, 0.9, 0)
    l = tp(None, logits)
    if tp is None:
        print(tp)