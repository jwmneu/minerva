import math
import sys
import time
import numpy as np
import owl
from net import Net
from net_helper import CaffeNetBuilder
from caffe import *
from PIL import Image

class NetTrainer:
    ''' Class for DNN training.

    :ivar str solver_file: name of the solver_file, it will tell Minerva the network configuration and model saving path 
    :ivar snapshot: continue training from the snapshot under model saving path. If no model is saved under that snapshot folder, Minerva will randomly initial the weights according to configure file and training from scratch
    :ivar num_gpu: Minerva support training with multiple gpus and update weights synchronously
    
    '''
    
    def __init__(self, solver_file, snapshot, num_gpu = 1):
        self.solver_file = solver_file
        self.snapshot = snapshot
        self.num_gpu = num_gpu
        self.gpu = [owl.create_gpu_device(i) for i in range(num_gpu)]

    def build_net(self):
        self.owl_net = Net()
        self.builder = CaffeNetBuilder(self.solver_file)
        self.snapshot_dir = self.builder.snapshot_dir
        self.builder.build_net(self.owl_net, self.num_gpu)
        self.owl_net.compute_size()
        self.builder.init_net_from_file(self.owl_net, self.snapshot_dir, self.snapshot)

    def run(s):
        wgrad = [[] for i in range(s.num_gpu)]
        bgrad = [[] for i in range(s.num_gpu)]
        last = time.time()
        wunits = s.owl_net.get_weighted_unit_ids()
        last_start = time.time()

        for iteridx in range(s.snapshot * s.owl_net.solver.snapshot, s.owl_net.solver.max_iter):
            # get the learning rate
            if s.owl_net.solver.lr_policy == "poly":
                s.owl_net.current_lr = s.owl_net.base_lr * pow(1 - float(iteridx) / s.owl_net.solver.max_iter, s.owl_net.solver.power)
            elif s.owl_net.solver.lr_policy == "step":
                s.owl_net.current_lr = s.owl_net.base_lr * pow(s.owl_net.solver.gamma, iteridx / s.owl_net.solver.stepsize)

            # train on multi-gpu
            for gpuid in range(s.num_gpu):
                owl.set_device(s.gpu[gpuid])
                s.owl_net.forward('TRAIN')
                s.owl_net.backward('TRAIN')
                for wid in wunits:
                    wgrad[gpuid].append(s.owl_net.units[wid].weightgrad)
                    bgrad[gpuid].append(s.owl_net.units[wid].biasgrad)

            # weight update
            for i in range(len(wunits)):
                wid = wunits[i]
                upd_gpu = i * s.num_gpu / len(wunits)
                owl.set_device(s.gpu[upd_gpu])
                for gid in range(s.num_gpu):
                    if gid == upd_gpu:
                        continue
                    wgrad[upd_gpu][i] += wgrad[gid][i]
                    bgrad[upd_gpu][i] += bgrad[gid][i]
                s.owl_net.units[wid].weightgrad = wgrad[upd_gpu][i]
                s.owl_net.units[wid].biasgrad = bgrad[upd_gpu][i]
                s.owl_net.update(wid)

            if iteridx % 2 == 0:
                owl.wait_for_all()
                thistime = time.time() - last
                print "Finished training %d minibatch (time: %s)" % (iteridx, thistime)
                last = time.time()

            wgrad = [[] for i in range(s.num_gpu)] # reset gradients
            bgrad = [[] for i in range(s.num_gpu)]

            # decide whether to display loss
            if (iteridx + 1) % (s.owl_net.solver.display) == 0:
                lossunits = s.owl_net.get_loss_units()
                for lu in lossunits:
                    print "Training Loss %s: %f" % (lu.name, lu.getloss())

            # decide whether to test
            if (iteridx + 1) % (s.owl_net.solver.test_interval) == 0:
                acc_num = 0
                test_num = 0
                for testiteridx in range(s.owl_net.solver.test_iter[0]):
                    s.owl_net.forward('TEST')
                    all_accunits = s.owl_net.get_accuracy_units()
                    accunit = all_accunits[len(all_accunits)-1]
                    #accunit = all_accunits[0]
                    test_num += accunit.batch_size
                    acc_num += (accunit.batch_size * accunit.acc)
                    print "Accuracy the %d mb: %f" % (testiteridx, accunit.acc)
                    sys.stdout.flush()
                print "Testing Accuracy: %f" % (float(acc_num)/test_num)

            # decide whether to save model
            if (iteridx + 1) % (s.owl_net.solver.snapshot) == 0:
                print "Save to snapshot %d, current lr %f" % ((iteridx + 1) / (s.owl_net.solver.snapshot), s.owl_net.current_lr)
                s.builder.save_net_to_file(s.owl_net, s.snapshot_dir, (iteridx + 1) / (s.owl_net.solver.snapshot))
            sys.stdout.flush()

class MultiviewTester:
    ''' Class for multiview testing.

    Multiview testing will get better accuracy than single view testing. For each image, it will crop out the left-top, right-top, left-down, right-down, central patches and their hirizontal flipped version. The final prediction is averaged according to the 10 views.

    :ivar str solver_file: name of the solver_file, it will tell Minerva the network configuration and model saving path 
    :ivar str softmax_layer_name: name of the softmax layer that produce prediction 
    :ivar snapshot: saved model snapshot index
    :ivar gpu: the gpu to run testing

    '''
    
    def __init__(self, solver_file, softmax_layer_name, snapshot, gpu_idx = 0):
        self.solver_file = solver_file
        self.softmax_layer_name = softmax_layer_name
        self.snapshot = snapshot
        self.gpu = owl.create_gpu_device(gpu_idx)
        owl.set_device(self.gpu)

    def build_net(self):
        self.owl_net = Net()
        self.builder = CaffeNetBuilder(self.solver_file)
        self.snapshot_dir = self.builder.snapshot_dir
        self.builder.build_net(self.owl_net)
        self.owl_net.compute_size('MULTI_VIEW')
        self.builder.init_net_from_file(self.owl_net, self.snapshot_dir, self.snapshot)

    def run(s):
        #multi-view test
        acc_num = 0
        test_num = 0
        loss_unit = s.owl_net.units[s.owl_net.name_to_uid[s.softmax_layer_name][0]] 
        for testiteridx in range(s.owl_net.solver.test_iter[0]):
            for i in range(10): 
                s.owl_net.forward('MULTI_VIEW')
                if i == 0:
                    softmax_val = loss_unit.ff_y
                    batch_size = softmax_val.shape[1]
                    softmax_label = loss_unit.y
                else:
                    softmax_val = softmax_val + loss_unit.ff_y
            
            test_num += batch_size
            predict = softmax_val.argmax(0)
            truth = softmax_label.argmax(0)
            correct = (predict - truth).count_zero()
            acc_num += correct
            print "Accuracy the %d mb: %f, batch_size: %d" % (testiteridx, correct, batch_size)
            sys.stdout.flush()
        print "Testing Accuracy: %f" % (float(acc_num)/test_num)

class FeatureExtractor:
    ''' Class of feature extractor.
    Feature will be stored in a txt file as a matrix. The size of the feature matrix is [num_img, feature_dimension]

    :ivar str solver_file: name of the solver_file, it will tell Minerva the network configuration and model saving path 
    :ivar snapshot: saved model snapshot index
    :ivar str layer_name: name of the ayer that produce feature 
    :ivar str feature_path: the file path to save feature
    :ivar gpu: the gpu to run testing

    '''
    
    
    def __init__(self, solver_file, snapshot, layer_name, feature_path, gpu_idx = 0):
        self.solver_file = solver_file
        self.snapshot = snapshot
        self.layer_name = layer_name
        self.feature_path = feature_path
        self.gpu = owl.create_gpu_device(gpu_idx)
        owl.set_device(self.gpu)

    def build_net(self):
        self.owl_net = Net()
        self.builder = CaffeNetBuilder(self.solver_file)
        self.snapshot_dir = self.builder.snapshot_dir
        self.builder.build_net(self.owl_net)
        self.owl_net.compute_size('TEST')
        self.builder.init_net_from_file(self.owl_net, self.snapshot_dir, self.snapshot)

    def run(s):
        feature_unit = s.owl_net.units[s.owl_net.name_to_uid[s.layer_name][0]] 
        feature_file = open(s.feature_path, 'w')
        batch_dir = 0
        for testiteridx in range(s.owl_net.solver.test_iter[0]):
            s.owl_net.forward('TEST')
            feature = feature_unit.out.to_numpy()
            feature_shape = np.shape(feature)
            img_num = feature_shape[0]
            feature_length = np.prod(feature_shape[1:len(feature_shape)])
            feature = np.reshape(feature, [img_num, feature_length])
            for imgidx in range(img_num):
                for feaidx in range(feature_length):
                    info ='%f ' % (feature[imgidx, feaidx])
                    feature_file.write(info)
                feature_file.write('\n')
            print "Finish One Batch %d" % (batch_dir)
            batch_dir += 1
        feature_file.close()

class FilterVisualizer:
    ''' Class of filter visualizer.
    Find the most interested patches of a filter to demostrate pattern that filter insterested in. 

    :ivar str solver_file: name of the solver_file, it will tell Minerva the network configuration and model saving path 
    :ivar snapshot: saved model snapshot index
    :ivar str layer_name: name of the ayer that produce feature 
    :ivar str result_path: path for the result of visualization
    :ivar gpu: the gpu to run testing

    '''
    
    
    def __init__(self, solver_file, snapshot, layer_name, result_path, gpu_idx = 0):
        self.solver_file = solver_file
        self.snapshot = snapshot
        self.layer_name = layer_name
        self.result_path = result_path
        self.gpu = owl.create_gpu_device(gpu_idx)
        owl.set_device(self.gpu)

    def build_net(self):
        self.owl_net = Net()
        self.builder = CaffeNetBuilder(self.solver_file)
        self.snapshot_dir = self.builder.snapshot_dir
        self.builder.build_net(self.owl_net)
        self.owl_net.compute_size('TEST')
        self.builder.init_net_from_file(self.owl_net, self.snapshot_dir, self.snapshot)

    def run(s):
        #Need Attention, here we may have multiple data layer, just choose the TEST layer
        data_unit = None
        for i in range(len(s.owl_net.name_to_uid['data'])):
            if s.owl_net.units[s.owl_net.name_to_uid['data'][i]].params.include[0].phase == 1:
                data_unit = s.owl_net.units[s.owl_net.name_to_uid['data'][i]]
        assert(data_unit)
       
        bp = BlobProto()
        #get mean file
        if len(data_unit.params.transform_param.mean_file) == 0:
            mean_data = np.ones([3, 256, 256], dtype=np.float32)
            assert(len(data_unit.params.transform_param.mean_value) == 3)
            mean_data[0] = data_unit.params.transform_param.mean_value[0]
            mean_data[1] = data_unit.params.transform_param.mean_value[1]
            mean_data[2] = data_unit.params.transform_param.mean_value[2]
            h_w = 256
        else:    
            with open(data_unit.params.transform_param.mean_file, 'rb') as f:
                bp.ParseFromString(f.read())
            mean_narray = np.array(bp.data, dtype=np.float32)
            h_w = np.sqrt(np.shape(mean_narray)[0] / 3)
            mean_data = np.array(bp.data, dtype=np.float32).reshape([3, h_w, h_w])
        #get the cropped img
        crop_size = data_unit.params.transform_param.crop_size
        crop_h_w = (h_w - crop_size) / 2
        mean_data = mean_data[:, crop_h_w:crop_h_w + crop_size, crop_h_w:crop_h_w + crop_size]

        feature_unit = s.owl_net.units[s.owl_net.name_to_uid[s.layer_name][0]] 
        batch_dir = 0
        all_data = None
        all_feature = None
        for testiteridx in range(s.owl_net.solver.test_iter[0]):
            print batch_dir
            s.owl_net.forward('TEST')
            feature = feature_unit.out.to_numpy()
            if all_feature == None:
                all_feature = feature
            else:
                all_feature = np.concatenate((all_feature, feature), axis=0)
            data = data_unit.out.to_numpy()
            if all_data == None:
                all_data = data
            else:
                all_data = np.concatenate((all_data, data), axis=0)
            batch_dir += 1
            #HACK TODO: only take 10000 images
            if np.shape(all_feature)[0] >= 10000:
                break


        #get the result 
        feature_shape = feature_unit.out_shape
        patch_shape = feature_unit.rec_on_ori
        min_val = -float('inf') 
        
        #add back the mean file
        for i in range(np.shape(all_data)[0]):
            all_data[i,:,:,:] += mean_data
       
        if len(feature_shape) == 4:
            #iter for each filter, for each filter, we choose nine patch from different image
            for i in range(feature_shape[2]):
                #create the result image for nine patches
                res_img = np.zeros([feature_unit.rec_on_ori * 3, feature_unit.rec_on_ori * 3, 3])
                filter_feature = np.copy(all_feature[:,i,:,:])
                for patchidx in range(9):
                    maxidx = np.argmax(filter_feature)
                    colidx = maxidx % feature_shape[0]
                    maxidx = (maxidx - colidx) / feature_shape[0]
                    rowidx = maxidx % feature_shape[1]
                    maxidx = (maxidx - rowidx) / feature_shape[1]
                    imgidx = maxidx
                    info = '%d %d %d' % (imgidx, rowidx, colidx)
                    filter_feature[imgidx,:,:] = min_val
                    
                    #get the patch place
                    patch_start_row = max(0,feature_unit.start_on_ori + rowidx * feature_unit.stride_on_ori)
                    patch_end_row = min(feature_unit.start_on_ori + rowidx * feature_unit.stride_on_ori + feature_unit.rec_on_ori, data_unit.crop_size)

                    patch_start_col = max(0,feature_unit.start_on_ori + colidx * feature_unit.stride_on_ori)
                    patch_end_col = min(feature_unit.start_on_ori + colidx * feature_unit.stride_on_ori + feature_unit.rec_on_ori, data_unit.crop_size)
                    patch = all_data[imgidx, :, patch_start_row:patch_end_row, patch_start_col:patch_end_col]

                    #save img to image
                    row_in_res = patchidx / 3
                    col_in_res = patchidx % 3
                    st_row = row_in_res * patch_shape 
                    st_col = col_in_res * patch_shape
                    #turn gbr into rgb
                    res_img[st_row:st_row+patch_end_row - patch_start_row, st_col:st_col + patch_end_col - patch_start_col, 2] = patch[0,:,:]
                    res_img[st_row:st_row+patch_end_row - patch_start_row, st_col:st_col + patch_end_col - patch_start_col, 1] = patch[1,:,:]
                    res_img[st_row:st_row+patch_end_row - patch_start_row, st_col:st_col + patch_end_col - patch_start_col, 0] = patch[2,:,:]

                #save img
                res_img = Image.fromarray(res_img.astype(np.uint8))
                res_path = '%s/%d.jpg' % (s.result_path, i)
                print res_path
                res_img.save(res_path, format = 'JPEG')

        else:
            assert(False)










