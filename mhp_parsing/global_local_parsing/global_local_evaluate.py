#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@Author  :   Peike Li
@Contact :   peike.li@yahoo.com
@File    :   evaluate.py
@Time    :   8/4/19 3:36 PM
@Desc    :
@License :   This source code is licensed under the license found in the
             LICENSE file in the root directory of this source tree.
"""

import os
import argparse
import numpy as np
import torch
import cv2

from torch.utils import data
from tqdm import tqdm
from PIL import Image as PILImage
import torchvision.transforms as transforms
import torch.backends.cudnn as cudnn

import sys
sys.path.append('/home/qiu/Projects/Self-Correction-Human-Parsing/')
import networks
from utils.miou import compute_mean_ioU
from utils.transforms import BGR2RGB_transform
from utils.transforms import transform_parsing, transform_logits
from global_local_parsing.global_local_datasets import CropDataValSet


def get_arguments():
    """Parse all the arguments provided from the CLI.

    Returns:
      A list of parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Self Correction for Human Parsing")

    # Network Structure
    parser.add_argument("--arch", type=str, default='resnet101_3')
    # Data Preference
    parser.add_argument("--data-dir",
                        type=str,
                        default='mhp_extension/data/DemoDataset')
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--split-name", type=str, default='crop_pic')
    parser.add_argument("--input-size", type=str, default='473,473')
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--ignore-label", type=int, default=255)
    parser.add_argument("--random-mirror", action="store_true")
    parser.add_argument("--random-scale", action="store_true")
    # Evaluation Preference
    parser.add_argument(
        "--log-dir",
        type=str,
    )
    parser.add_argument(
        "--model-restore",
        type=str,
        default=
        '/home/qiu/Downloads/models/detectron2/exp_schp_multi_cihp_local.pth')
    parser.add_argument("--gpu",
                        type=str,
                        default='0',
                        help="choose gpu device.")
    parser.add_argument("--save-results",
                        default=True,
                        action="store_true",
                        help="whether to save the results.")
    parser.add_argument("--flip",
                        action="store_true",
                        help="random flip during the test.")
    parser.add_argument("--multi-scales",
                        type=str,
                        default='1',
                        help="multiple scales during the test")
    return parser.parse_args()


def get_palette(num_cls):
    """ Returns the color map for visualizing the segmentation mask.
    Args:
        num_cls: Number of classes
    Returns:
        The color map
    """
    n = num_cls
    palette = [0] * (n * 3)
    for j in range(0, n):
        lab = j
        palette[j * 3 + 0] = 0
        palette[j * 3 + 1] = 0
        palette[j * 3 + 2] = 0
        i = 0
        while lab:
            palette[j * 3 + 0] |= (((lab >> 0) & 1) << (7 - i))
            palette[j * 3 + 1] |= (((lab >> 1) & 1) << (7 - i))
            palette[j * 3 + 2] |= (((lab >> 2) & 1) << (7 - i))
            i += 1
            lab >>= 3
    return palette


def multi_scale_testing(model,
                        batch_input_im,
                        crop_size=[473, 473],
                        flip=True,
                        multi_scales=[1]):
    flipped_idx = (15, 14, 17, 16, 19, 18)
    if len(batch_input_im.shape) > 4:
        batch_input_im = batch_input_im.squeeze()
    if len(batch_input_im.shape) == 3:
        batch_input_im = batch_input_im.unsqueeze(0)

    interp = torch.nn.Upsample(size=crop_size,
                               mode='bilinear',
                               align_corners=True)
    ms_outputs = []
    for s in multi_scales:
        interp_im = torch.nn.Upsample(scale_factor=s,
                                      mode='bilinear',
                                      align_corners=True)
        scaled_im = interp_im(batch_input_im)
        parsing_output = model(scaled_im)
        parsing_output = parsing_output[0][-1]
        output = parsing_output[0]
        # print(output.shape)
        if flip:
            flipped_output = parsing_output[1]
            flipped_output[14:20, :, :] = flipped_output[flipped_idx, :, :]
            output += flipped_output.flip(dims=[-1])
            output *= 0.5
        output = interp(output.unsqueeze(0))
        ms_outputs.append(output[0])
    ms_fused_parsing_output = torch.stack(ms_outputs)
    ms_fused_parsing_output = ms_fused_parsing_output.mean(0)
    ms_fused_parsing_output = ms_fused_parsing_output.permute(1, 2, 0)  # HWC
    parsing = torch.argmax(ms_fused_parsing_output, dim=2)
    parsing = parsing.data.cpu().numpy()
    ms_fused_parsing_output = ms_fused_parsing_output.data.cpu().numpy()
    return parsing, ms_fused_parsing_output


def glparsing(data_dir, split_name, schp_ckpt, log_dir, file_list):
    """Create the model and start the evaluation process."""
    args = get_arguments()
    multi_scales = [float(i) for i in args.multi_scales.split(',')]
    gpus = [int(i) for i in args.gpu.split(',')]
    assert len(gpus) == 1
    if not args.gpu == 'None':
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    cudnn.benchmark = True
    cudnn.enabled = True

    h, w = map(int, args.input_size.split(','))
    input_size = [h, w]

    model = networks.init_model(args.arch,
                                num_classes=args.num_classes,
                                pretrained=None)

    IMAGE_MEAN = model.mean
    IMAGE_STD = model.std
    INPUT_SPACE = model.input_space
    print('image mean: {}'.format(IMAGE_MEAN))
    print('image std: {}'.format(IMAGE_STD))
    print('input space:{}'.format(INPUT_SPACE))
    if INPUT_SPACE == 'BGR':
        print('BGR Transformation')
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=IMAGE_MEAN, std=IMAGE_STD),
        ])
    if INPUT_SPACE == 'RGB':
        print('RGB Transformation')
        transform = transforms.Compose([
            transforms.ToTensor(),
            BGR2RGB_transform(),
            transforms.Normalize(mean=IMAGE_MEAN, std=IMAGE_STD),
        ])

    # Data loader
    local_test_dataset = CropDataValSet(data_dir,
                                        split_name[0],
                                        crop_size=input_size,
                                        transform=transform,
                                        flip=args.flip)
    local_num_samples = len(local_test_dataset)
    print('local testing sample numbers: {}'.format(local_num_samples))
    local_testloader = data.DataLoader(local_test_dataset,
                                       batch_size=args.batch_size,
                                       shuffle=False,
                                       pin_memory=True)

    global_test_dataset = CropDataValSet(data_dir,
                                         split_name[1],
                                         crop_size=input_size,
                                         transform=transform,
                                         flip=args.flip)
    global_num_samples = len(global_test_dataset)
    print('global testing sample numbers: {}'.format(global_num_samples))
    global_testloader = data.DataLoader(global_test_dataset,
                                        batch_size=args.batch_size,
                                        shuffle=False,
                                        pin_memory=True)

    # Load model weight
    state_dict = torch.load(schp_ckpt)
    model.load_state_dict(state_dict)

    model.cuda()
    model.eval()

    local_sp_results_dir = os.path.join(log_dir, split_name[0] + '_parsing')
    if not os.path.exists(local_sp_results_dir):
        os.makedirs(local_sp_results_dir)

    global_sp_results_dir = os.path.join(log_dir, split_name[1] + '_parsing')
    if not os.path.exists(global_sp_results_dir):
        os.makedirs(global_sp_results_dir)

    palette = get_palette(20)
    parsing_preds = []
    # local_scales = np.zeros((local_num_samples, 2), dtype=np.float32)
    # local_centers = np.zeros((local_samples, 2), dtype=np.int32)
    with torch.no_grad():
        for idx, meta in file_list:
            src_name = meta['im_name']
            src = cv2.imread(os.path.join(data_dir, 'src_imgs', src_name),
                             cv2.IMREAD_COLOR)
            parsing_results=[]
            for i in range(len(meta['person_bbox']) + 1):
                if i == 0:
                    img = src
                else:
                    x_min, y_min, x_max, y_max = meta['person_bbox'][i - 1]
                    img = src[y_min:y_max + 1, x_min:x_max + 1, :]
                h, w, _ = img.shape
                c = [h / 2, w / 2]
                temps = max(w, h) - 1
                s = np.array([temps * 1.0, temps * 1.0], dtype=np.float32)
                parsing, logits = multi_scale_testing(
                    model,
                    img.cuda(),
                    crop_size=input_size,
                    flip=args.flip,
                    multi_scales=multi_scales)
                parsing[parsing != 2] = 0
                parsing_result = transform_parsing(parsing, c, s, w, h,
                                                   input_size)
                parsing_results.append(parsing_result)
            meta['parsing_results']=parsing_results
            

    with torch.no_grad():
        for idx, batch in enumerate(tqdm(local_testloader)):
            image, meta = batch
            if (len(image.shape) > 4):
                image = image.squeeze()
            im_name = meta['name'][0]
            c = meta['center'].numpy()[0]
            s = meta['scale'].numpy()[0]
            w = meta['width'].numpy()[0]
            h = meta['height'].numpy()[0]
            # scales[idx, :] = s
            # centers[idx, :] = c
            parsing, logits = multi_scale_testing(model,
                                                  image.cuda(),
                                                  crop_size=input_size,
                                                  flip=args.flip,
                                                  multi_scales=multi_scales)

            if args.save_results:
                # parsing_result = transform_parsing(parsing, c, s, w, h, input_size)
                # parsing_result_path = os.path.join(local_sp_results_dir, im_name + '.png')
                # output_im = PILImage.fromarray(np.asarray(parsing_result, dtype=np.uint8))
                # output_im.putpalette(palette)
                # output_im.save(parsing_result_path)

                # save logits
                logits_result = transform_logits(logits, c, s, w, h,
                                                 input_size)
                logits_result_path = os.path.join(local_sp_results_dir,
                                                  im_name + '.npy')
                np.save(logits_result_path, logits_result)

        for idx, batch in enumerate(tqdm(global_testloader)):
            image, meta = batch
            if (len(image.shape) > 4):
                image = image.squeeze()
            im_name = meta['name'][0]
            c = meta['center'].numpy()[0]
            s = meta['scale'].numpy()[0]
            w = meta['width'].numpy()[0]
            h = meta['height'].numpy()[0]
            # scales[idx, :] = s
            # centers[idx, :] = c
            parsing, logits = multi_scale_testing(model,
                                                  image.cuda(),
                                                  crop_size=input_size,
                                                  flip=args.flip,
                                                  multi_scales=multi_scales)

            if args.save_results:
                # parsing_result = transform_parsing(parsing, c, s, w, h, input_size)
                # parsing_result_path = os.path.join(global_sp_results_dir, im_name + '.png')
                # output_im = PILImage.fromarray(np.asarray(parsing_result, dtype=np.uint8))
                # output_im.putpalette(palette)
                # output_im.save(parsing_result_path)
                # save logits
                logits_result = transform_logits(logits, c, s, w, h,
                                                 input_size)
                logits_result_path = os.path.join(global_sp_results_dir,
                                                  im_name + '.npy')
                np.save(logits_result_path, logits_result)
    return


if __name__ == '__main__':
    main()
