import math
import numpy as np
import torch
import torch.nn.functional as F
from sklearn.metrics import roc_auc_score
from torch.nn import BCEWithLogitsLoss, Conv1d, MaxPool1d, ModuleList
from torch_geometric.data import InMemoryDataset
from torch_geometric.loader import DataLoader
from torch_geometric.nn import MLP, GraphConv, global_sort_pool
from torch_geometric.utils import from_networkx
import networkx as nx
from sklearn.model_selection import train_test_split
import torch_geometric.transforms as T
import pandas as pd
import random
import netlsd
import json


# dataset is available here: https://github.com/benedekrozemberczki/datasets#twitch-ego-nets


dataset = json.load(open("data/twitch_egos_2/twitch_egos_2.json"))
targets = pd.read_csv('data/twitch_egos_2/twitch_target.csv')



graphs, labels = [],[]
for k, edge_list in dataset.items():
    index = int(k)
    g = nx.Graph()
    g.add_edges_from(edge_list)
    deg = dict(nx.degree(g))
    g.remove_node( max(deg, key=deg.get))
    y = targets['target'].iloc[index]
    graphs.append(g)
    labels.append(y)
    if index%1000==0:
        print(index)


#compute max degree
max_degree=max_n = 0
for g in graphs:
    n = g.number_of_nodes()
    
    deg = max(dict(nx.degree(g)).values())
    if deg>max_degree:
        max_degree = deg
    if n>max_n:
        max_n = n
print(max_degree, max_n)
max_degree = max_degree+1
def get_netlsd(graph, n,k):
    ego_graph = nx.ego_graph(graph, n, radius=k)
    if ego_graph:
        des = netlsd.heat(ego_graph, timescales = np.logspace(-2, 2, 20))
    else: 
        des = np.random.normal(0.0,1.0,(20))
    return des



## create dataset with train and test splits
class MyOwnDataset(InMemoryDataset):
    def __init__(self, dataset, labels,k, split='train'):
        self.dataset = dataset
        self.labels = labels
        self.k = k
#         self.num_hops = num_hops
        super().__init__("data/twitch_egos_2/")
        index = ['train', 'test'].index(split)
        self.data, self.slices = torch.load(self.processed_paths[index])

    @property
    def processed_file_names(self):
        return ['train_twitch_netlsd.pt', 'test_twitch_netlsd.pt']
    def process(self):
        train_list, test_list = [],[]
        train_indices, test_indices = train_test_split(list(range(len(self.labels))), test_size=0.2, stratify=self.labels, random_state=123)
        print("#training sampels:", len(train_indices))
        for i, index in enumerate(train_indices):
            graph = self.dataset[index]
            X = []
            for n in graph.nodes():
                emb = get_netlsd(graph,n,self.k)
                X.append(emb)
            d = from_networkx(graph)
            d.x = torch.tensor(X).float()
            d.y = torch.tensor(labels[index]).float()
            train_list.append(d)
            if i%100==0:
                print(i)
                
        
        for index in test_indices:
            graph = self.dataset[index]
            X = []
            for n in graph.nodes():
                emb = get_netlsd(graph,n,self.k)
                X.append(emb)
            d = from_networkx(graph)
            d.x = torch.tensor(X).float()
            d.y = torch.tensor(labels[index]).float()
            test_list.append(d)
        
#         ### DEGREE
#         for i, index in enumerate(train_indices):
#             graph = self.dataset[index]
#             N = graph.number_of_nodes()
#             degree = dict(nx.degree(graph))
#             X = np.zeros((N, max_degree),dtype = float)
#             for j,n in enumerate(graph.nodes()):
#                 deg = degree.get(n)
#                 subg = nx.ego_graph(graph,n,self.k)
#                 X[j,deg] = 1
#             d = from_networkx(graph)
#             d.x = torch.tensor(X).float()
#             d.y = torch.tensor(labels[index]).float()
#             train_list.append(d)
#             if i%1000==0:
#                 print(i)
                
                
#         for i, index in enumerate(test_indices):
#             graph = self.dataset[index]
#             N = graph.number_of_nodes()
#             degree = dict(nx.degree(graph))
#             X = np.zeros((N, max_degree),dtype = float)
#             for j,n in enumerate(graph.nodes()):
#                 deg = degree.get(n)
#                 subg = nx.ego_graph(graph,n,self.k)
#                 X[j,deg] = 1
#             d = from_networkx(graph)
#             d.x = torch.tensor(X).float()
#             d.y = torch.tensor(labels[index]).float()
#             test_list.append(d)
#             if i%1000==0:
#                 print(i)
        torch.save(self.collate(train_list),
                   self.processed_paths[0])
        torch.save(self.collate(test_list),
                   self.processed_paths[1])

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
import warnings
warnings.filterwarnings('ignore')
k =3
train_dataset = MyOwnDataset(graphs,labels, k,split='train')
test_dataset = MyOwnDataset(graphs, labels,k, split='test')
batch_size = 128
train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
test_loader = DataLoader(test_dataset, batch_size=batch_size)

class DGCNN(torch.nn.Module):
    def __init__(self, hidden_channels,num_layers,GNN,k=0.6):
        super().__init__()

        if k < 1:  # Transform percentile to number.
            num_nodes = sorted([data.num_nodes for data in train_dataset])
            k = num_nodes[int(math.ceil(k * len(num_nodes))) - 1]
            k = max(10, k)
        self.k = int(k)

        self.convs = ModuleList()
        self.convs.append(GNN(train_dataset.num_features, hidden_channels))
        for i in range(0, num_layers - 1):
            self.convs.append(GNN(hidden_channels, hidden_channels))
        self.convs.append(GNN(hidden_channels, 1))
        conv1d_channels = [16, 32]
        total_latent_dim = hidden_channels * num_layers + 1
        conv1d_kws = [total_latent_dim, 5]
        self.conv1 = Conv1d(1, conv1d_channels[0], conv1d_kws[0],
                            conv1d_kws[0])
        self.maxpool1d = MaxPool1d(2, 2)
        self.conv2 = Conv1d(conv1d_channels[0], conv1d_channels[1],
                            conv1d_kws[1], 1)
        dense_dim = int((self.k - 2) / 2 + 1)
        dense_dim = (dense_dim - conv1d_kws[1] + 1) * conv1d_channels[1]
        self.mlp = MLP([dense_dim, 32, 1], dropout=0.5, batch_norm=False)

    def forward(self, x, edge_index, batch):
        xs = [x]
        for conv in self.convs:
            xs += [conv(xs[-1], edge_index).tanh()]
        x = torch.cat(xs[1:], dim=-1)

        # Global pooling.
        x = global_sort_pool(x, batch, self.k)
        
        x = x.unsqueeze(1)  # [num_graphs, 1, k * hidden]
        x = self.conv1(x).relu()
        x = self.maxpool1d(x)
        x = self.conv2(x).relu()
        x = x.view(x.size(0), -1)  # [num_graphs, dense_dim]
        return self.mlp(x)



def train():
    model.train()
    total_loss = 0
    for data in train_loader:
        data = data.to(device)
        optimizer.zero_grad()
        out = model(data.x, data.edge_index, data.batch)
        loss = criterion(out.view(-1), data.y.to(torch.float))
        loss.backward()
        optimizer.step()
        total_loss += float(loss) * data.num_graphs

    return total_loss / len(train_dataset)
def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)


@torch.no_grad()
def test(loader):
    model.eval()
    y_pred, y_true = [], []
    for data in loader:
        data = data.to(device)
        logits = model(data.x, data.edge_index, data.batch)
        y_pred.append(logits.view(-1).cpu())
        y_true.append(data.y.view(-1).cpu().to(torch.float))

    return roc_auc_score(torch.cat(y_true), torch.cat(y_pred))

gnn = eval("GraphConv")
epochs = 100
hidden_channels,num_layers=64, 3
seeds = [123]
for seed in seeds:
    set_seed(seed)
    model = DGCNN(hidden_channels, num_layers,gnn).to(device)
    optimizer = torch.optim.Adam(params=model.parameters(), lr=0.0001)
    criterion = torch.nn.BCEWithLogitsLoss()

    for epoch in range(epochs):
        loss = train()
        test_acc = test(test_loader)
    #         test_acc = test(test_loader)
        
        print(f'Epoch: {epoch:02d}, Loss: {loss:.4f}, '
                f'Test: {test_acc:.4f}')
        

