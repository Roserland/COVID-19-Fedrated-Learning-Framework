# -*- coding: utf-8 -*-
import os
import sys
import json
import torch
import logging
import numpy as np
import torch.nn as nn
import torch.optim as optim

from apex import amp
from fl_client import FL_Client
from model.model import densenet3d
from train import train, add_weight_decay
from common import TrainDataset, DataLoader, WarmUpLR, Logger


if __name__ == '__main__':

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

    client = FL_Client('./config/client2_config.json')
    client.start()
    client.register()

    # *********************** initialize train params *******************************
    with open('./config/train_config_client2.json') as j:
        train_config = json.load(j)

    # set up the training dataloader
    train_data_train = TrainDataset(train_config['train_data_dir'],
                                    train_config['train_df_csv'],
                                    train_config['labels_train_df_csv'])
    train_data_loader = DataLoader(dataset=train_data_train,
                                   batch_size=train_config['train_batch_size'],
                                   shuffle=True,
                                   num_workers=train_config['num_workers'])

    # set up the model
    device = 'cuda' if train_config['use_cuda'] else 'cpu'
    model = densenet3d().to(device)
    weight = torch.from_numpy(np.array([[0.2, 0.2, 0.4, 0.2]])).float()
    criterion = nn.CrossEntropyLoss(weight=weight).to(device)

    # add no bias decay
    params = add_weight_decay(model, 4e-5)
    optimizer = optim.SGD(params, lr=train_config['lr'], momentum=train_config['momentum'])
    model, optimizer = amp.initialize(model, optimizer)
    model = nn.DataParallel(model)
    iter_per_epoch, warm_epoch = len(train_data_loader), 5
    warmup_scheduler = WarmUpLR(optimizer, iter_per_epoch * warm_epoch)
    train_scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, train_config['epoch'] - warm_epoch)

    # set up the log file
    logfile = "./train_valid_client2.log"
    os.remove(logfile) if os.path.exists(logfile) else None
    sys.stdout = Logger(logfile)
    log = logging.getLogger()

    # *********************** training start *******************************
    for epoch_num in range(client.configs['iteration']):
        logger.info('***** current epoch is {} *****'.format(epoch_num))
        recv_filePath = client.train_model_path
        print(recv_filePath)

        # unpack the .pth file, and decrypt model_state & weight num
        _model_state, _weight_sum, _client_num = client.unpack_param(_model_param_path=recv_filePath)
        dec_model_state = client.decrypt(_model_state, _client_num)
        dec_weight_num = _weight_sum  # not encrypted

        print("weight num calculated from server:{}".format(1.0 / dec_weight_num))
        print("some of decrypted parameters:")
        temp_key = list(dec_model_state.keys())[0]
        print(dec_model_state[temp_key][0]) 
        # assign weighted state to the model
        client.set_weight(1.0)
        for key in dec_model_state.keys():
            dec_model_state[key] = dec_model_state[key] * (float(_client_num) / dec_weight_num)
        torch.save(dec_model_state, './{}_current.pth'.format(client.configs['username']))
        print("After Decryption\n", dec_model_state[temp_key][0])

        # *********************** train part-2 begin *******************************
        # load the state dict of new model
        model.load_state_dict(dec_model_state)
        fileName = 'model_Param_{}.pth'.format(client.configs['username'])
        update_name = train(filename=fileName, device=device, train_data_loader=train_data_loader,
                            model=model, optimizer=optimizer, log=log, warm_epoch=warm_epoch,
                            epoch=1, criterion=criterion, warmup_scheduler=warmup_scheduler,
                            train_scheduler=train_scheduler, save_interval=5, save_folder='./model/')
        # *********************** train part-2 end *******************************

        # encryption, then send
        trained_model_state_dict = torch.load(update_name)
        print("After training, some state")
        print(trained_model_state_dict[temp_key][0])
        print("**********************\n*****************************\n")
        print("client weight {}\n".format(client.weight))
        for key in trained_model_state_dict.keys():
            trained_model_state_dict[key] = trained_model_state_dict[key] * client.weight

        enc_model_state = client.encrypt(trained_model_state_dict)
        dec_model_again = client.decrypt(enc_model_state, 1)
        print("some test decrypted state:", dec_model_again[temp_key][0])
        # client.set_weight(1.0)
        # enc_client_weight = client.enc_num(client.weight)
        _model_Param = {"model_state_dict": enc_model_state,
                        "client_weight": client.weight}
        savePath = os.path.join(client.configs["models_dir"],
                                './model_Param_{}.pth'.format(client.configs['username'],))
        torch.save(_model_Param, savePath)
        client.send_model(model_weight_path=savePath, versionNum=epoch_num+1)

    logger.info("training finished")
    client.stop()
