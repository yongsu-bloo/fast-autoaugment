import copy
import os
import sys
import time
from collections import OrderedDict, defaultdict
from collections.abc import Iterable

import torch

import numpy as np
from hyperopt import hp
import ray
import gorilla
from ray.tune.trial import Trial
from ray.tune.trial_runner import TrialRunner
from ray.tune.suggest.hyperopt import HyperOptSearch
from ray.tune.suggest import ConcurrencyLimiter
from ray.tune import register_trainable, run_experiments
from tqdm import tqdm

from pathlib import Path
lib_dir = (Path("__file__").parent).resolve()
if str(lib_dir) not in sys.path: sys.path.insert(0, str(lib_dir))
from FastAutoAugment.archive import remove_deplicates, policy_decoder
from FastAutoAugment.augmentations import augment_list
from FastAutoAugment.common import get_logger, add_filehandler
from FastAutoAugment.data import get_dataloaders
from FastAutoAugment.metrics import Accumulator
from FastAutoAugment.networks import get_model, num_class
from FastAutoAugment.train import train_and_eval
from theconf import Config as C, ConfigArgumentParser

VERSION = 1
NUM_GROUP = 5
top1_valid_by_cv = defaultdict(lambda: list)

def gen_assign_group(version, num_group=5):
    if version == 1:
        return assign_group
    elif version == 2:
        return lambda data, label=None: assign_group2(data,label,num_group)
    elif version == 3:
        return lambda data, label=None: assign_group3(data,label,num_group)

def assign_group(data, label=None):
    """
    input: data(batch of images), label(optional, same length with data)
    return: assigned group numbers for data (same length with data)
    to be used before training B.O.
    """
    classes = ('plane', 'car', 'bird', 'cat', 'deer', 'dog', 'frog', 'horse', 'ship', 'truck')
    # exp1: group by manualy classified classes
    groups = {}
    groups[0] = ['plane', 'ship']
    groups[1] = ['car', 'truck']
    groups[2] = ['horse', 'deer']
    groups[3] = ['dog', 'cat']
    groups[4] = ['frog', 'bird']
    def _assign_group_id1(_label):
        gr_id = None
        for key in groups:
            if classes[_label] in groups[key]:
                gr_id = key
                return gr_id
        if gr_id is None:
            raise ValueError(f"label {_label} is not given properly. classes[label] = {classes[_label]}")
    if not isinstance(label, Iterable):
        return _assign_group_id1(label)
    return list(map(_assign_group_id1, label))

def assign_group2(data, label=None, num_group=5):
    """
    input: data(batch of images), label(optional, same length with data)
    return: randomly assigned group numbers for data (same length with data)
    to be used before training B.O.
    """
    _size = len(data)
    return np.random.randint(0, num_group, size=_size)

def assign_group3(data, label=None, num_group=5):
    classes = ('plane', 'car', 'bird', 'cat', 'deer', 'dog', 'frog', 'horse', 'ship', 'truck')
    _classes = list(copy.deepcopy(classes))
    np.random.shuffle(_classes)
    groups = defaultdict(lambda: [])
    num_cls_per_gr = len(_classes) // num_group + 1
    for i in range(num_group):
        for _ in range(num_cls_per_gr):
            if len(_classes) == 0:
                break
            groups[i].append(_classes.pop())

    def _assign_group_id1(_label):
        gr_id = None
        for key in groups:
            if classes[_label] in groups[key]:
                gr_id = key
                return gr_id
        if gr_id is None:
            raise ValueError(f"label {_label} is not given properly. classes[label] = {classes[_label]}")
    if not isinstance(label, Iterable):
        return _assign_group_id1(label)
    return list(map(_assign_group_id1, label))

def step_w_log(self):
    original = gorilla.get_original_attribute(ray.tune.trial_runner.TrialRunner, 'step')

    # log
    cnts = OrderedDict()
    for status in [Trial.RUNNING, Trial.TERMINATED, Trial.PENDING, Trial.PAUSED, Trial.ERROR]:
        cnt = len(list(filter(lambda x: x.status == status, self._trials)))
        cnts[status] = cnt
    best_top1_acc = 0.
    for trial in filter(lambda x: x.status == Trial.TERMINATED, self._trials):
        if not trial.last_result:
            continue
        best_top1_acc = max(best_top1_acc, trial.last_result['top1_valid'])
    print('iter', self._iteration, 'top1_acc=%.3f' % best_top1_acc, cnts, end='\r')
    return original(self)


patch = gorilla.Patch(ray.tune.trial_runner.TrialRunner, 'step', step_w_log, settings=gorilla.Settings(allow_hit=True))
gorilla.apply(patch)


logger = get_logger('Fast AutoAugment')


def _get_path(dataset, model, tag, basemodel=True):
    base_path = "models" if basemodel else f"models/{C.get()['exp_name']}"
    os.makedirs(base_path, exist_ok=True)
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), '%s/%s_%s_%s.model' % (base_path, dataset, model, tag))     # TODO


@ray.remote(num_gpus=1)
def train_model(config, dataloaders, dataroot, augment, cv_ratio_test, gr_id, save_path=None, skip_exist=False, gr_assign=None):
    C.get()
    C.get().conf = config
    C.get()['aug'] = augment
    result = train_and_eval(None, dataloaders, dataroot, cv_ratio_test, gr_id, save_path=save_path, only_eval=skip_exist, gr_assign=gr_assign)
    return C.get()['model']['type'], gr_id, result

def eval_tta(config, augment, reporter):
    C.get()
    C.get().conf = config
    cv_ratio_test, gr_id, save_path = augment['cv_ratio_test'], augment['gr_id'], augment['save_path']

    # setup - provided augmentation rules
    C.get()['aug'] = policy_decoder(augment, augment['num_policy'], augment['num_op'])

    # eval
    model = get_model(C.get()['model'], num_class(C.get()['dataset']))
    ckpt = torch.load(save_path)
    if 'model' in ckpt:
        model.load_state_dict(ckpt['model'])
    else:
        model.load_state_dict(ckpt)
    model.eval()

    loaders = []
    for _ in range(augment['num_policy']):  # TODO
        _, tl, validloader, tl2 = get_dataloaders(C.get()['dataset'], C.get()['batch'], augment['dataroot'], cv_ratio_test, split_idx=gr_id, gr_assign=gen_assign_group(version=VERSION, num_group=NUM_GROUP))
        loaders.append(iter(validloader))
        del tl, tl2

    start_t = time.time()
    metrics = Accumulator()
    loss_fn = torch.nn.CrossEntropyLoss(reduction='none')
    try:
        while True:
            losses = []
            corrects = []
            for loader in loaders:
                data, label = next(loader)
                data = data.cuda()
                label = label.cuda()

                pred = model(data)

                loss = loss_fn(pred, label)
                losses.append(loss.detach().cpu().numpy().reshape(-1))

                _, pred = pred.topk(1, 1, True, True)
                pred = pred.t()
                correct = pred.eq(label.view(1, -1).expand_as(pred)).detach().cpu().numpy().reshape(-1)
                corrects.append(correct)
                del loss, correct, pred, data, label

            losses = np.concatenate(losses)
            losses_min = np.min(losses, axis=0).squeeze()

            corrects = np.concatenate(corrects)
            corrects_max = np.max(corrects, axis=0).squeeze()
            metrics.add_dict({
                'minus_loss': -1 * np.sum(losses_min),
                'correct': np.sum(corrects_max),
                'cnt': len(losses)
            })
            del corrects, corrects_max
    except StopIteration:
        pass

    del model
    metrics = metrics / 'cnt'
    gpu_secs = (time.time() - start_t) * torch.cuda.device_count()
    reporter(minus_loss=metrics['minus_loss'], top1_valid=metrics['correct'], elapsed_time=gpu_secs, done=True)
    return metrics['correct']


if __name__ == '__main__':
    import json
    from pystopwatch2 import PyStopwatch
    w = PyStopwatch()

    parser = ConfigArgumentParser(conflict_handler='resolve')
    parser.add_argument('--dataroot', type=str, default='/mnt/ssd/data/', help='torchvision data folder')
    parser.add_argument('--until', type=int, default=5)
    parser.add_argument('--num-op', type=int, default=2)
    parser.add_argument('--num-policy', type=int, default=5)
    parser.add_argument('--num-search', type=int, default=200)
    parser.add_argument('--cv-ratio', type=float, default=0.4)
    parser.add_argument('--decay', type=float, default=-1)
    parser.add_argument('--redis', type=str)
    parser.add_argument('--per-class', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--smoke-test', action='store_true')
    # parser.add_argument('--cv-num', type=int, default=5)
    parser.add_argument('--exp_name', type=str)
    parser.add_argument('--gr-num', type=int, default=5)
    parser.add_argument('--random', action='store_true')
    parser.add_argument('--rpc', type=int, default=10)
    args = parser.parse_args()
    C.get()['exp_name'] = args.exp_name
    if args.decay > 0:
        logger.info('decay=%.4f' % args.decay)
        C.get()['optimizer']['decay'] = args.decay

    add_filehandler(logger, os.path.join('models', '%s_%s_cv%.1f.log' % (C.get()['dataset'], C.get()['model']['type'], args.cv_ratio)))
    logger.info('configuration...')
    logger.info(json.dumps(C.get().conf, sort_keys=True, indent=4))
    logger.info('initialize ray...')
    ray.init(address=args.redis)

    num_process_per_gpu = 2
    num_result_per_cv = args.rpc
    gr_num = args.gr_num
    # assert gr_num == 5, "version1 requires gr-num == 5."
    C.get()["cv_num"] = gr_num
    copied_c = copy.deepcopy(C.get().conf)

    logger.info('search augmentation policies, dataset=%s model=%s' % (C.get()['dataset'], C.get()['model']['type']))
    logger.info('----- Train without Augmentations cv=%d ratio(test)=%.1f -----' % (gr_num, args.cv_ratio))
    w.start(tag='train_no_aug')
    paths = [_get_path(C.get()['dataset'], C.get()['model']['type'], 'ratio%.1f_fold%d' % (args.cv_ratio, i)) for i in range(gr_num)]
    print(paths)
    reqs = [
        train_model.remote(copy.deepcopy(copied_c), None, args.dataroot, C.get()['aug'], args.cv_ratio, i, save_path=paths[i], skip_exist=True)
        for i in range(gr_num)]

    tqdm_epoch = tqdm(range(C.get()['epoch']))
    is_done = False
    for epoch in tqdm_epoch:
        while True:
            epochs_per_cv = OrderedDict()
            for cv_idx in range(gr_num):
                try:
                    latest_ckpt = torch.load(paths[cv_idx])
                    if 'epoch' not in latest_ckpt:
                        epochs_per_cv['cv%d' % (cv_idx + 1)] = C.get()['epoch']
                        continue
                    epochs_per_cv['cv%d' % (cv_idx+1)] = latest_ckpt['epoch']
                except Exception as e:
                    continue
            tqdm_epoch.set_postfix(epochs_per_cv)
            if len(epochs_per_cv) == gr_num and min(epochs_per_cv.values()) >= C.get()['epoch']:
                is_done = True
            if len(epochs_per_cv) == gr_num and min(epochs_per_cv.values()) >= epoch:
                break
            time.sleep(10)
        if is_done:
            break

    logger.info('getting results...')
    pretrain_results = ray.get(reqs)
    for r_model, r_cv, r_dict in pretrain_results:
        logger.info('model=%s cv=%d top1_train=%.4f top1_valid=%.4f' % (r_model, r_cv+1, r_dict['top1_train'], r_dict['top1_valid']))
    logger.info('processed in %.4f secs' % w.pause('train_no_aug'))

    if args.until == 1:
        sys.exit(0)

    logger.info('----- Search Test-Time Augmentation Policies -----')
    w.start(tag='search')

    ops = augment_list(False)
    space = {}
    for i in range(args.num_policy):
        for j in range(args.num_op):
            space['policy_%d_%d' % (i, j)] = hp.choice('policy_%d_%d' % (i, j), list(range(0, len(ops))))
            space['prob_%d_%d' % (i, j)] = hp.uniform('prob_%d_ %d' % (i, j), 0.0, 1.0)
            space['level_%d_%d' % (i, j)] = hp.uniform('level_%d_ %d' % (i, j), 0.0, 1.0)

    final_policy_group = defaultdict(lambda : [])
    total_computation = 0
    reward_attr = 'top1_valid'      # top1_valid or minus_loss
    for _ in range(1):  # run multiple times.
        for gr_id in range(gr_num):
            final_policy_set = []
            name = "search_%s_%s_group%d_%d_ratio%.1f" % (C.get()['dataset'], C.get()['model']['type'], gr_id, gr_num, args.cv_ratio)
            print(name)
            register_trainable(name, lambda augs, reporter: eval_tta(copy.deepcopy(copied_c), augs, reporter))
            algo = HyperOptSearch(space, metric=reward_attr)
            algo = ConcurrencyLimiter(algo, max_concurrent=num_process_per_gpu*8)
            exp_config = {
                name: {
                    'run': name,
                    'num_samples': 4 if args.smoke_test else args.num_search,
                    'resources_per_trial': {'gpu': 1./num_process_per_gpu},
                    'stop': {'training_iteration': args.num_policy},
                    'config': {
                        'dataroot': args.dataroot, 'save_path': paths[gr_id],
                        'cv_ratio_test': args.cv_ratio, 'gr_id': gr_id,
                        'num_op': args.num_op, 'num_policy': args.num_policy
                    },
                }
            }
            results = run_experiments(exp_config, search_alg=algo, scheduler=None, verbose=0, queue_trials=True, resume=args.resume, raise_on_failed_trial=False)
            print()
            results = [x for x in results if x.last_result]
            results = sorted(results, key=lambda x: x.last_result[reward_attr], reverse=True)
            # calculate computation usage
            for result in results:
                total_computation += result.last_result['elapsed_time']

            for result in results[:num_result_per_cv]:
                final_policy = policy_decoder(result.config, args.num_policy, args.num_op)
                logger.info('loss=%.12f top1_valid=%.4f %s' % (result.last_result['minus_loss'], result.last_result['top1_valid'], final_policy))

                final_policy = remove_deplicates(final_policy)
                final_policy_set.extend(final_policy)
            final_policy_group[gr_id].extend(final_policy_set)
            probs = []
            for aug in final_policy_set:
                for op in aug:
                    probs.append(op[1])
            prob = sum(probs) / len(probs)
            print("mean_prob: {:.2f}".format(prob))

    logger.info(json.dumps(final_policy_group))
    logger.info('processed in %.4f secs, gpu hours=%.4f' % (w.pause('search'), total_computation / 3600.))
    logger.info('----- Train with Augmentations model=%s dataset=%s aug=%s ratio(test)=%.1f -----' % (C.get()['model']['type'], C.get()['dataset'], C.get()['aug'], args.cv_ratio))
    w.start(tag='train_aug')
    #@TODO: training data -> augmentation
    # raise NotImplementedError
    num_experiments = 4
    default_path = [_get_path(C.get()['dataset'], C.get()['model']['type'], 'ratio%.1f_default%d' % (args.cv_ratio, _), basemodel=False) for _ in range(num_experiments)]
    augment_path = [_get_path(C.get()['dataset'], C.get()['model']['type'], 'ratio%.1f_augment%d' % (args.cv_ratio, _), basemodel=False) for _ in range(num_experiments)]
    reqs = [train_model.remote(copy.deepcopy(copied_c), None, args.dataroot, C.get()['aug'], 0.0, 0, save_path=default_path[_], skip_exist=True) for _ in range(num_experiments)] + \
        [train_model.remote(copy.deepcopy(copied_c), None, args.dataroot, final_policy_group, 0.0, 0, save_path=augment_path[_], gr_assign=gen_assign_group(version=VERSION, num_group=NUM_GROUP)) for _ in range(num_experiments)]

    tqdm_epoch = tqdm(range(C.get()['epoch']))
    is_done = False
    for epoch in tqdm_epoch:
        while True:
            epochs = OrderedDict()
            for exp_idx in range(num_experiments):
                try:
                    if os.path.exists(default_path[exp_idx]):
                        latest_ckpt = torch.load(default_path[exp_idx])
                        epochs['default_exp%d' % (exp_idx + 1)] = latest_ckpt['epoch']
                except:
                    pass
                try:
                    if os.path.exists(augment_path[exp_idx]):
                        latest_ckpt = torch.load(augment_path[exp_idx])
                        epochs['augment_exp%d' % (exp_idx + 1)] = latest_ckpt['epoch']
                except:
                    pass

            tqdm_epoch.set_postfix(epochs)
            if len(epochs) == num_experiments*2 and min(epochs.values()) >= C.get()['epoch']:
                is_done = True
            if len(epochs) == num_experiments*2 and min(epochs.values()) >= epoch:
                break
            time.sleep(10)
        if is_done:
            break

    logger.info('getting results...')
    final_results = ray.get(reqs)

    for train_mode in ['default', 'augment']:
        avg = 0.
        for _ in range(num_experiments):
            r_model, r_cv, r_dict = final_results.pop(0)
            logger.info('[%s] top1_train=%.4f top1_test=%.4f' % (train_mode, r_dict['top1_train'], r_dict['top1_test']))
            avg += r_dict['top1_test']
        avg /= num_experiments
        logger.info('[%s] top1_test average=%.4f (#experiments=%d)' % (train_mode, avg, num_experiments))
    logger.info('processed in %.4f secs' % w.pause('train_aug'))

    logger.info(w)
