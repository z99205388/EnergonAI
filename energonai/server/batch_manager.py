"""
------------------------------------------
Class Batch Manager and the function for generating cached cost.
This code modifies the batch wrapping algorithm of Turbo Transformer.
------------------------------------------
"""
import time

import torch.cuda
from scipy import stats
import numpy as np
from energonai.engine import InferenceEngine
import random
import redis
import os
from tqdm import trange
import threading
from readerwriterlock import rwlock
import logging
from concurrent.futures import ThreadPoolExecutor


class single_request:

    def __init__(self, input_, time_stamp: float, input_str: str):
        """
        class to store related information for a single request.
        :param input_: The output of GPT2Tokenizer.tokenizer, a dict including input_ids and attention_mask
        :param time_stamp: The time stamp when we receive the request. We use the time stamp as a index to
                            identify the request.
        :param input_str: The input string of the request.
        """
        self.input_ = input_
        self.text = input_str
        self.time_ = time_stamp
        self.seq_len = input_['input_ids'].shape[1]


class Manager:
    """
    Base class of batch manager.
    """

    def __init__(self):
        pass

    def insert_req(self, time_stamp: float, input_ids, input_str: str):
        pass


class Batch_Manager(Manager):
    """
    This batch manager is mainly used for maintaining a queue of request to be processed. The requests in the
    queue is wrapped into batches according to the sequence length and the priority calculated with the equation
    in function cal_priority and then sent into the inference engine.
    """

    def __init__(self,
                 engine: InferenceEngine,
                 model_name: str,
                 pp: int,
                 tp: int,
                 max_sequence_length: int,
                 init_mu: int = 512,
                 init_theta: int = 180,
                 max_batch_size: int = 32,
                 lr: float = 0.01,
                 tokenizer=None,
                 pad_token=None,
                 rm_padding=False,
                 step: int = 1,
                 repeat_round: int = 3):
        """
        :param engine: The InferenceEngine from energonai.engine
        :param cached_cost: The output of function generate_cached_cost
        :param init_mu: initial mean value we suppose for incoming sequence length.
        :param init_theta: initial variance value we suppose for incoming sequence length.
        :param max_batch_size: the max number of requests that can be wrapped into one batch.
        :param lr: the learning rate we use to update the mean and variance that we suppose for the normal
                    distribution of sequence length.
        """
        super().__init__()
        self.engine = engine
        self.max_batch_size = max_batch_size
        self.lr = lr
        self.mu = init_mu
        self.theta = init_theta
        self.req_list = []
        self.req_list_lock = rwlock.RWLockFair()
        self.write_lock = self.req_list_lock.gen_wlock()
        self.cached_cost = self.generate_cached_cost(engine,
                                                     model_name,
                                                     pp=pp,
                                                     tp=tp,
                                                     max_seq_len=max_sequence_length,
                                                     max_batch_size=max_batch_size,
                                                     step=step,
                                                     repeat_round=repeat_round,
                                                     tokenizer=tokenizer)
        self.tokenizer = tokenizer
        self.rm_padding = rm_padding
        if self.tokenizer and pad_token:
            self.tokenizer.pad_token = pad_token    # GPT2Tokenizer.eos_token
        self.running_flag = True
        self.publisher = redis.StrictRedis('localhost', 6379, charset="utf-8", decode_responses=True)
        self.pool = ThreadPoolExecutor(max_workers=pp+1) # a small thread pool can keep the message queue not in a crowded state.
        self.main_thread = threading.Thread(target=self.processing_batch)
        self.main_thread.start()

    def generate_cached_cost(self,
                             engine,
                             model_name: str,
                             pp: int,
                             tp: int,
                             max_seq_len: int = 1024,
                             max_batch_size: int = 16,
                             step: int = 1,
                             repeat_round: int = 3,
                             tokenizer=None):
        """
        Test the running time for different sequence length and batch size on the current machine.
        :param engine: InferenceEngine from energonai.engine
        :type engine: InferenceEngine
        :param max_seq_len: The max sequence length that is measured.
        :param max_batch_size: The max batch size that is measured.
        :param step: Run time is measured every other 'step' of sequence length
        :param repeat_round: We inference current batch 'repeat_round' times and take average.
        """
        logging.log(0, "fetching cached cost")
        cached_name = "cached_cost_{}_pp{}_tp{}_{}_{}_{}_{}.npy".format(model_name, pp, tp, max_seq_len, max_batch_size,
                                                                        step, repeat_round)
        if os.path.exists(cached_name):
            logging.log(0, "loading cached cost from file")
            cached_cost = np.load(cached_name).tolist()
        else:
            logging.log(0, "generating new cached cost")
            cached_cost = [[0 for i in range(max_batch_size + 1)] for j in range(max_seq_len + 1)]
            warm_up_str = "test test test"
            if tokenizer:
                warm_up_input = tokenizer(warm_up_str, return_tensors="pt")
            else:
                warm_up_input = warm_up_str
            for tt in range(5):
                output = engine.run(warm_up_input)
                predictions = output.to_here()
            input_text = ""
            for tmp_len in trange(1, max_seq_len + 1, step):
                input_text += "test "
                for tmp_batch in range(1, max_batch_size + 1):
                    batched_text = [input_text for _ in range(tmp_batch)]
                    if tokenizer:
                        input_token = tokenizer(batched_text, return_tensors="pt")
                    else:
                        input_token = batched_text
                    output = engine.run(input_token)
                    predictions = output.to_here()
                    if tokenizer:
                        tokenizer.decode(predictions)
                    start_time = time.time()
                    for k in range(repeat_round):
                        if tokenizer:
                            input_token = tokenizer(batched_text, return_tensors="pt")
                        else:
                            input_token = batched_text
                        output = engine.run(input_token)
                        predictions = output.to_here()
                        if tokenizer:
                            tokenizer.decode(predictions)
                    time_cost = (time.time() - start_time) / repeat_round
                    cached_cost[tmp_len][tmp_batch] = time_cost
                    for k in range(1, step):
                        cached_cost[tmp_len + k][tmp_batch] = time_cost
            np.save(cached_name, np.array(cached_cost))
        logging.log(0, "cached cost loaded")
        return cached_cost

    def subscribe_result(self, time_stamp):
        # red = redis.StrictRedis('localhost', 6379, charset="utf-8", decode_responses=True)
        # sub = red.pubsub()
        sub = self.publisher.pubsub()
        sub.subscribe(str(time_stamp))
        predictions = ''
        for message in sub.listen():
            if message is not None and isinstance(message, dict):
                predictions = message.get('data')
                if not isinstance(predictions, int):
                    break
        return predictions

    def insert_req(self, time_stamp: float, input_ids, input_str: str):
        """
        Build a single_request class with the input string and then insert it into the queue.
        """
        tmp_req = single_request(input_ids, time_stamp, input_str)
        self.write_lock.acquire()
        self.req_list.append(tmp_req)
        self.req_list.sort(key=lambda x: x.seq_len)
        self.write_lock.release()

    def cal_priority(self, batch_list: list, cur_stamp: float):
        """
        Given a wrapped batch, calculate its priority to decide which batch to be given to the inference engine.
        The equation is based on the sequence length, batch size and the max wait time among the batch.
        We suppose that the length of the requests follows a normal distribution, so for the batches with a
        length that has a higher possibility to appear, we tend to let it wait a little longer for other requests
        with similar length in order to increase the batch size.
        The batches with larger batch size also gains higher priority.
        In order to avoid starving problem, we use exponential function to raise the priority of batches which
        have waited for long.
        """
        cur_len = batch_list[-1].seq_len
        earliest_timestamp = min([i.time_ for i in batch_list])

        wait_time = cur_stamp - earliest_timestamp
        batch_size = len(batch_list)
        # appear_possibility_weight = 1.0 / self.cal_norm_weight(cur_len)

        # TODO adjust the euqation
        priority = batch_size * np.exp(2 * wait_time * wait_time)
        return priority

    def cal_norm_weight(self, seq_len):
        """
        Approximately estimate the possibility of a certain sequence length using normal distribution.
        """
        return stats.norm(self.mu, self.theta).cdf(seq_len) - \
               stats.norm(self.mu, self.theta).cdf(seq_len - 1)

    def update_norm(self, batch_: list):
        """
        Every time we are done inserting a request into the inference engine, we update mu and theta of our
        distribution with the current batch and the pre-set learning rate.
        """
        new_mu = np.mean([i.seq_len for i in batch_])
        delta_mu = new_mu - self.mu
        self.mu += self.lr * delta_mu
        temp_batch = np.array([i.seq_len - self.mu for i in batch_])
        new_theta = np.sqrt(np.mean(temp_batch**2))
        delta_theta = new_theta - self.theta
        self.theta += self.lr * delta_theta
        return

    def wrap_batch(self):
        """
        Given a sorted sequence list, calculate the best way to wrap the batch with DP according to the
        cached cost.
        The algorithm in this function comes from the paper of Turbo Transformer.
        """
        self.write_lock.acquire()
        states = [0]
        start_idx_list = [0]
        for i in range(1, len(self.req_list) + 1):
            j = i - 1
            start_idx = i - 1
            cur_length = self.req_list[i - 1].seq_len
            min_cost = self.cached_cost[cur_length][1] + states[j]
            while j > max(0, i - self.max_batch_size):
                tmp_cost = states[j - 1] + \
                           self.cached_cost[cur_length][i - j + 1]
                if tmp_cost < min_cost:
                    min_cost = tmp_cost
                    start_idx = j - 1
                j -= 1
            states.append(min_cost)
            start_idx_list.append(start_idx)
        i = len(self.req_list)
        res_start = -1
        res_end = -1
        max_priority = -1
        cur_timestamp = time.time()
        while i > 0:
            end_idx = i
            start_idx = start_idx_list[i]
            current_batch = self.req_list[start_idx:end_idx]
            current_priority = self.cal_priority(current_batch, cur_timestamp)
            if current_priority > max_priority:
                max_priority = current_priority
                res_start = start_idx
                res_end = end_idx
            i = start_idx - 1
        result_batch = self.req_list[res_start:res_end]
        del self.req_list[res_start:res_end]
        self.write_lock.release()
        return result_batch

    def processing_batch(self):
        """
        The background process that continuously calls wrap_batch, puts the batch into the inference engine,
        and starts new processes that wait for and publish the inference result.
        """
        while self.running_flag:

            if len(self.req_list) > 0:
                target_batch = self.wrap_batch()
                pad_len = target_batch[-1].seq_len
                logging.info("A batch with {} requests and length of {} packed, in-batch length: {}".format(
                    len(target_batch), pad_len, [p.seq_len for p in target_batch]))
                input_text = [i.text for i in target_batch]
                if self.tokenizer:
                    input_ids = self.tokenizer(input_text, padding="longest", return_tensors="pt")
                else:
                    input_ids = input_text
                if self.rm_padding:
                    input_ids['seq_lens'] = torch.Tensor([p.seq_len for p in target_batch])
                output = self.engine.run(input_ids)
                self.pool.submit(self.publish_result, output, target_batch)
                # self.update_norm(target_batch)
                # self.publish_result(output, target_batch, start_time)
                # pub_thread = threading.Thread(target=self.publish_result, args=(output, target_batch, start_time))
                # pub_thread.start()
            time.sleep(0.05)

    def publish_result(self, output, target_batch):
        """
        Background process that waits for the inference result and uses the publisher of Redis to publish it to
        the waiting requests.
        :param output: the rpc reference of the inference result.
        :param target_batch: the input batch
        """
        predictions = output.to_here()
        for i in range(len(target_batch)):
            temp_st = target_batch[i].time_
            chosen_pred = predictions[i]
            if self.tokenizer:
                text_ = self.tokenizer.decode(int(chosen_pred))
            else:
                text_ = chosen_pred
            self.publisher.publish(str(temp_st), text_)
