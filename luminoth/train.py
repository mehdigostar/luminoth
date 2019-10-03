r"""PyTorch Detection Training.

To run in a multi-gpu environment, use the distributed launcher::

    python -m torch.distributed.launch --nproc_per_node=$NGPU --use_env \
        train.py ... --world-size $NGPU

The default hyperparameters are tuned for training on 8 gpus and 2 images per gpu.
    --lr 0.02 --batch-size 2 --world-size 8
If you use different number of gpus, the learning rate should be changed to 0.02/8*$NGPU.
"""
import datetime
import os
import time
import warnings

import torch
import torch.utils.data
import torchvision

from datasets.coco_utils import get_coco, get_coco_kp

from datasets.group_by_aspect_ratio import GroupedBatchSampler, create_aspect_ratio_groups
from engine import train_one_epoch, evaluate

import utils
import transforms as T
from urllib.parse import urlparse

from models import detection

def get_dataset(name, image_set, transform, data_path, gs_bucket=None):
    if name == "coco":
        ds_fn, num_classes = get_coco, 91
    elif name == "coco_kp":
        ds_fn, num_classes = get_coco_kp, 2
    else:
        print("Dataset must be 'coco' or 'coco_kp'")
        exit()

    ds = ds_fn(data_path, image_set=image_set, transforms=transform, gs_bucket=gs_bucket)
    return ds, num_classes


def get_transform(train):
    transforms = []
    transforms.append(T.ToTensor())
    if train:
        transforms.append(T.RandomHorizontalFlip(0.5))
    return T.Compose(transforms)


def print_cuda_devices_info(local_gpus):
    num_cuda_devices = torch.cuda.device_count()
    if num_cuda_devices:
        print("CUDA GPUs found in this machine:")
        for i in range(num_cuda_devices):
            device_name = torch.cuda.get_device_name(i)
            device_mega_bytes = round(
                torch.cuda.get_device_properties(i).total_memory / (1024 ** 2)
            )
            print("  {}: {} ({}MB) {}".format(
                i, device_name, device_mega_bytes,
                "- Selected" if i in local_gpus else ""))
    else:
        print("No CUDA GPUs found in this machine.")


def main_worker(local_rank, args):
    utils.init_distributed_mode(local_rank, args)

    # Check if dataset is in google cloud storage
    url_path = urlparse(args.data_path)
    if url_path.scheme == 'gs':
        from google.cloud import storage
        # https://github.com/googleapis/google-auth-library-python/issues/271
        warnings.filterwarnings(
            "ignore", "Your application has authenticated using end user credentials"
        )
        client = storage.Client()
        args.data_path = url_path.path[1:]  # Remove leftover '/'
        bucket_name = url_path.netloc
        gs_bucket = client.get_bucket(bucket_name)
        print(f"Streaming datasets from Google Cloud Storage bucket named '{bucket_name}'")
    else:
        gs_bucket = None

    dataset, num_classes = get_dataset(
        args.dataset, "train", get_transform(train=True), args.data_path, gs_bucket
    )
    dataset_test, _ = get_dataset(
        args.dataset, "val", get_transform(train=False), args.data_path, gs_bucket
    )

    # print("Creating data loaders")
    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
        test_sampler = torch.utils.data.distributed.DistributedSampler(dataset_test)
    else:
        train_sampler = torch.utils.data.RandomSampler(dataset)
        test_sampler = torch.utils.data.SequentialSampler(dataset_test)

    if args.aspect_ratio_group_factor >= 0:
        group_ids = create_aspect_ratio_groups(dataset, k=args.aspect_ratio_group_factor)
        train_batch_sampler = GroupedBatchSampler(train_sampler, group_ids, args.batch_size)
    else:
        train_batch_sampler = torch.utils.data.BatchSampler(
            train_sampler, args.batch_size, drop_last=True)

    data_loader = torch.utils.data.DataLoader(
        dataset, batch_sampler=train_batch_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)

    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=1,
        sampler=test_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)

    print("Creating model")
    model = detection.__dict__[args.model](num_classes=num_classes, pretrained=args.pretrained)
    device = torch.device(args.device)
    model.to(device)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[local_rank])
        model_without_ddp = model.module

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(
        params, lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    # lr_scheduler = torch.optim.lr_scheduler.StepLR(
    #     optimizer, step_size=args.lr_step_size, gamma=args.lr_gamma)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
        optimizer, milestones=args.lr_steps, gamma=args.lr_gamma
    )

    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])

    if args.test_only:
        evaluate(model, data_loader_test, device=device)
        return

    print("Start training")
    start_time = time.time()
    for epoch in range(args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train_one_epoch(model, optimizer, data_loader, device, epoch, args.print_freq)
        lr_scheduler.step()
        if args.output_dir:
            utils.save_on_master({
                'model': model_without_ddp.state_dict(),
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': lr_scheduler.state_dict(),
                'args': args},
                os.path.join(args.output_dir, 'model_{}.pth'.format(epoch)))

        # evaluate after every epoch
        evaluate(model, data_loader_test, device=device)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description=__doc__)

    parser.add_argument('--data-path', default='/datasets01/COCO/022719/', help='dataset')
    parser.add_argument('--dataset', default='coco', help='dataset')
    parser.add_argument('--model', default='fasterrcnn_resnet50_fpn', help='model')
    parser.add_argument('--device', default='cuda', help='device')
    parser.add_argument('-b', '--batch-size', default=2, type=int,
                        help='images per gpu, the total batch size is $NGPU x batch_size')
    parser.add_argument('--epochs', default=13, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--lr', default=0.02, type=float,
                        help='initial learning rate, 0.02 is the default value for training '
                        'on 8 gpus and 2 images_per_gpu')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                        metavar='W', help='weight decay (default: 1e-4)',
                        dest='weight_decay')
    parser.add_argument('--lr-step-size', default=8, type=int,
                        help='decrease lr every step-size epochs')
    parser.add_argument('--lr-steps', default=[8, 11], nargs='+', type=int,
                        help='decrease lr every step-size epochs')
    parser.add_argument('--lr-gamma', default=0.1, type=float,
                        help='decrease lr by a factor of lr-gamma')
    parser.add_argument('--print-freq', default=20, type=int, help='print frequency')
    parser.add_argument('--output-dir', default='.', help='path where to save')
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--aspect-ratio-group-factor', default=0, type=int)
    parser.add_argument("--test-only",
                        dest="test_only",
                        help="Only test the model",
                        action="store_true")
    parser.add_argument("--pretrained",
                        dest="pretrained",
                        help="Use pre-trained models from the modelzoo",
                        action="store_true")

    # Distributed training parameters
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--node-rank', default=0, type=int,
                        help=('For identifying each machine(node), '
                              'should be consecutive: 0, 1, 2, ...'))
    parser.add_argument('--dist-url', default='env://',
                        help='url used to set up distributed training')
    parser.add_argument('--local-gpus', default=[0], nargs='+', type=int,
                        help='Which local gpus to use in this node')

    args = parser.parse_args()

    if args.output_dir:
        utils.mkdir(args.output_dir)

    if args.world_size == 1 and len(args.local_gpus) > 1:
        args.world_size = len(args.local_gpus)
    if args.world_size != 1 and args.world_size < len(args.local_gpus):
        print("--world-size should be equal or larger than"
              "the number of gpus selected in --local-gpus")
        exit()

    print_cuda_devices_info(args.local_gpus)

    if args.node_rank != 0 or len(args.local_gpus) > 1:
        from torch import multiprocessing
        multiprocessing.spawn(main_worker, nprocs=len(args.local_gpus), args=tuple([args]))
    else:
        main_worker(args.local_gpus[0], args)
