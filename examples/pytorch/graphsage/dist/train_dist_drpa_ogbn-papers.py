"""
Inductive Representation Learning on Large Graphs
Paper: http://papers.nips.cc/paper/6703-inductive-representation-learning-on-large-graphs.pdf
Code: https://github.com/williamleif/graphsage-simple
Simple reference implementation of GraphSAGE.

Modified:
\author Md. Vasimuddin <vasimuddin.md@intel.com>
"""
import os
import sys
import gc
import json
import argparse
import time
import numpy as np
import torch as th
import torch.nn as nn
import torch.nn.functional as F
import dgl
from dgl import DGLGraph
from dgl.data.utils import load_tensors, load_graphs
from dgl.data import register_data_args, load_data
from dgl.nn.pytorch.conv import SAGEConv

import torch.optim as optim
import torch.multiprocessing as mp
import torch.distributed as dist
from load_graph import load_reddit, load_ogb
from dgl.distgnn.drpa_sym import drpa

try:
    import torch_ccl
except ImportError as e:
    print(e)

class GraphSAGE(nn.Module):
    def __init__(self,
                 in_feats,
                 n_hidden,
                 n_classes,
                 n_layers,
                 activation,
                 dropout,
                 aggregator_type):
        super(GraphSAGE, self).__init__()
        self.layers = nn.ModuleList()
        self.dropout = nn.Dropout(dropout)
        self.activation = activation
        self.layers.append(SAGEConv(in_feats, n_hidden, aggregator_type))
        # hidden layers
        for i in range(n_layers - 1):
            self.layers.append(SAGEConv(n_hidden, n_hidden, aggregator_type))
        # output layer
        self.layers.append(SAGEConv(n_hidden, n_classes, aggregator_type))



    def forward(self, graph, inputs, inf=False):
        h = inputs
        for l, layer in enumerate(self.layers):
            h = layer(graph, h)
            if l != len(self.layers) - 1:
                h = self.activation(h)
                h = self.dropout(h)
       
        return h
    


def evaluate(model, graph, features, labels, nid):
    model.eval()
    with th.no_grad():
        logits = model(graph, features, True)
        logits = logits[nid]
        labels = labels[nid]
        _, indices = th.max(logits, dim=1)
        correct = th.sum(indices == labels)
        return correct.item() * 1.0 / len(labels)

def run(g, data):
    n_classes, node_map, num_parts, rank, world_size = data
    
    features = g.ndata['feat']
    labels = g.ndata['label']
    train_mask = g.ndata['train_mask']
    val_mask = g.ndata['val_mask']
    test_mask = g.ndata['test_mask']
    in_feats = features.shape[1]
    #n_classes = data.num_classes
    #n_edges = data.graph.number_of_edges()
    n_edges = g.number_of_edges()
    print("""----Data statistics------'
      #Nodes %d
      #Edges %d
      #Classes %d
      #Train samples %d
      #Val samples %d
      #Test samples %d""" %
          (g.number_of_nodes(), n_edges, n_classes,
           train_mask.int().sum().item(),
           val_mask.int().sum().item(),
           test_mask.int().sum().item()))

    if args.gpu < 0:
        cuda = False
    else:
        cuda = True
        th.cuda.set_device(args.gpu)
        features = features.cuda()
        labels = labels.cuda()
        train_mask = train_mask.cuda()
        val_mask = val_mask.cuda()
        test_mask = test_mask.cuda()
        print("use cuda:", args.gpu)

    train_nid = train_mask.nonzero().squeeze()
    val_nid = val_mask.nonzero().squeeze()
    test_nid = test_mask.nonzero().squeeze()

    # graph preprocess and calculate normalization factor
    n_edges = g.number_of_edges()
    if cuda:
        g = g.int().to(args.gpu)

    # create GraphSAGE model
    model = GraphSAGE(in_feats,
                      args.n_hidden,
                      n_classes,
                      args.n_layers,
                      F.relu,
                      args.dropout,
                      args.aggregator_type)
    
    if cuda:
        model.cuda()

    # use optimizer
    optimizer = th.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    # initialize graph
    dur = []
    for epoch in range(args.n_epochs):
        tic = time.time()
        model.train()

        # forward
        #tic_tf = time.time()
        logits = model(g, features)
        #toc_tf = time.time()
        loss = F.cross_entropy(logits[train_nid], labels[train_nid])

        optimizer.zero_grad()
        loss.backward()

        for param in model.parameters():
            if param.requires_grad and param.grad is not None:
                th.distributed.all_reduce(param.grad.data,
                                          op=th.distributed.ReduceOp.SUM)
        
        optimizer.step()
        if args.val:
            acc = evaluate(model, g, features, labels, val_nid)
            vacc = th.tensor(acc, dtype=th.float32)
            th.distributed.all_reduce(vacc, op=th.distributed.ReduceOp.SUM)
            if rank == 0:
                print("Epoch {:05d} | Time(s) {:.4f} | Loss {:.4f} | Accuracy {:.4f}"
                      .format(epoch, time.time() - tic, loss.item(),
                              float(vacc)/num_parts), flush=True)
        
        toc = time.time()
        if args.rank == 0:
            print("Epoch: {} time: {:0.4} sec"
                  .format(epoch, toc - tic), flush=True)
            print()
            

    print()
    acc = evaluate(model, g, features, labels, test_nid)
    cum_acc = th.tensor(acc, dtype=th.float32)
    th.distributed.all_reduce(cum_acc, op=th.distributed.ReduceOp.SUM)
    if args.rank == 0:
        print("----------------------------------------------------------------------", flush=True)
        print("Accuracy, avg: {:0.4f}%".format(float(cum_acc)/num_parts*100),
              flush=True)
        print("----------------------------------------------------------------------", flush=True)

    
def main(graph, data):
    gc.set_threshold(2, 2, 2)
    if args.rank == 0:
        print("Starting a run: ")
    run(graph, data)
    if args.rank == 0:
        print("Run completed !!!")

    
if __name__ == '__main__':
    th.set_printoptions(threshold=10)
    
    parser = argparse.ArgumentParser(description='GraphSAGE')
    register_data_args(parser)
    parser.add_argument("--dropout", type=float, default=0.5,
                        help="dropout probability")
    parser.add_argument("--gpu", type=int, default=-1,
                        help="gpu")
    ## nr=1 (cd-0), nr=5 (say, r=5), nr=-1 (0c)
    parser.add_argument("--nr", type=int, default=1,
                        help="#delay in delayed updates")
    parser.add_argument("--val", default=False,
                        action='store_true')
    parser.add_argument("--lr", type=float, default=0.01,
                        help="learning rate")
    parser.add_argument("--n-epochs", type=int, default=200,
                        help="number of training epochs")
    parser.add_argument("--n-hidden", type=int, default=64,
                        help="number of hidden gcn units")
    parser.add_argument("--n-layers", type=int, default=3,
                        help="number of hidden gcn layers")
    parser.add_argument("--weight-decay", type=float, default=5e-4,
                        help="Weight for L2 loss")
    parser.add_argument("--aggregator-type", type=str, default="gcn",
                        help="Aggregator type: mean/gcn/pool/lstm")
    parser.add_argument('--world-size', default=-1, type=int,
                         help='number of nodes for distributed training')
    parser.add_argument('--rank', default=-1, type=int,
                         help='node rank for distributed training')
    parser.add_argument('--dist-url', default='env://', type=str,
                         help='url used to set up distributed training')
    parser.add_argument('--dist-backend', default='ccl', type=str,
                            help='distributed backend')
    
    args = parser.parse_args()
    print(args)

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ.get("PMI_SIZE", -1))
        if args.world_size == -1: args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1
    if args.distributed:
        args.rank = int(os.environ.get("PMI_RANK", -1))
        if args.rank == -1: args.rank = int(os.environ["RANK"])
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    print("Rank: ", args.rank ," World_size: ", args.world_size)

    nc = args.world_size
    part_config = ""
    if args.dataset == 'ogbn-papers100M':
        part_config = os.path.join(part_config, "Libra_result_ogbn-papers100M", \
                                   str(nc) + "Communities", "ogbn-papers100M.json")
    else:
        raise DGLError("Error: Dataset not found.")

    if args.rank == 0:
        print("Dataset/partition location: ", part_config)
    with open(part_config) as conf_f:
        part_metadata = json.load(conf_f)

        
    part_files = part_metadata['part-{}'.format(args.rank)]
    assert 'node_feats' in part_files, "the partition does not contain node features."
    assert 'edge_feats' in part_files, "the partition does not contain edge feature."
    assert 'part_graph' in part_files, "the partition does not contain graph structure."
    node_feats = load_tensors(part_files['node_feats'])
    # edge_feats = load_tensors(part_files['edge_feats'])
    graph = load_graphs(part_files['part_graph'])[0][0]

    num_parts = part_metadata['num_parts']
    node_map  = part_metadata['node_map']

    graph.ndata['feat'] = node_feats['feat']
    graph.ndata['label'] = node_feats['label']
    graph.ndata['train_mask'] = node_feats['train_mask']
    graph.ndata['test_mask'] = node_feats['test_mask']
    graph.ndata['val_mask'] = node_feats['val_mask']

    graph.ndata['label'] = graph.ndata['label'].long()
    
    n_classes = 172
    if args.rank == 0:
        print("n_classes: ", n_classes, flush=True)

    data = n_classes, node_map, num_parts, args.rank, args.world_size
    gobj = drpa(graph, args.rank, num_parts, node_map, args.nr, dist, args.n_layers)
    main(gobj, data, args.dataset)
    gobj.drpa_finalize()
    
    if args.rank == 0:
        print("run details:")
        print("#ranks: ", args.world_size)
        print("Dataset:", args.dataset)
        print("Delay: ", args.nr)
        print("lr: ", args.lr)
        print("Aggregator: ", args.aggregator_type)
        print()
        print()

    dist.barrier()
