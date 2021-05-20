import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

"""
    Parameters:
        Vocab size: T 
        hidden_dim: hidden dimension
        input_dim (D_W): pre-trained using skip-gram model (300)     
"""

class CapsuleNetwork(nn.Module):
    def __init__(self, config, pretrained_embedding = None):
        super(CapsuleNetwork, self).__init__()
        self.hidden_size = config['hidden_size']
        self.vocab_size = config['vocab_size']
        self.word_emb_size = config['word_emb_size']
        self.learning_rate = config['learning_rate']
        self.batch_size = config['batch_size']

        self.word_embedding = nn.Embedding(config['vocab_size'], config['word_emb_size'])
        self.bilstm = nn.LSTM(config['word_emb_size'], config['hidden_size'],
                              config['nlayers'], bidirectional=True, batch_first=True)
        self.drop = nn.Dropout(config['keep_prob'])

        # parameters for self-attention
        self.n = config['max_time']
        self.d = config['word_emb_size']
        self.d_a = config['d_a']
        self.u = config['hidden_size']
        self.r = config['r']
        self.alpha = config['alpha']

        # attention
        self.ws1 = nn.Linear(config['hidden_size'] * 2, config['d_a'], bias=False)
        self.ws2 = nn.Linear(config['d_a'], config['r'], bias=False)
        self.tanh = nn.Tanh()
        self.softmax = nn.Softmax()

        self.s_cnum = config['s_cnum']
        self.margin = config['margin']
        self.keep_prob = config['keep_prob']
        self.num_routing = config['num_routing']
        self.output_atoms = config['output_atoms']
        self.nlayers = 2

        # for capsule
        self.input_dim = self.r
        self.input_atoms = self.hidden_size * 2
        self.output_dim = self.s_cnum
        self.capsule_weights = nn.Parameter(torch.zeros((self.r, self.hidden_size * 2,
                                                         self.s_cnum * self.output_atoms)))
        self.init_weights()


    def forward(self, input,len, embedding):
        self.s_len = len
        input = input.transpose(0,1) #(Bach,Length,D) => (L,B,D)
        # Attention
        if (embedding.nelement() != 0):
            self.word_embedding = nn.Embedding.from_pretrained(embedding)

        emb = self.word_embedding(input)
        packed_emb = pack_padded_sequence(emb, len)

        #Initialize hidden states
        h_0 = Variable(torch.zeros(4, input.shape[1], self.hidden_size))
        c_0 = Variable(torch.zeros(4, input.shape[1], self.hidden_size))

        outp = self.bilstm(packed_emb, (h_0, c_0))[0] ## [bsz, len, d_h * 2]
        outp = pad_packed_sequence(outp)[0].transpose(0,1).contiguous()
        size = outp.size()
        compressed_embeddings = outp.view(-1, size[2])  # [bsz * len, d_h * 2]
        hbar = self.tanh(self.ws1(self.drop(compressed_embeddings)))
        alphas = self.ws2(hbar).view(size[0], size[1], -1)  # [bsz, len, hop]

        self.attention = torch.transpose(alphas, 1, 2).contiguous()  # [bsz, hop, len]
        self.sentence_embedding = torch.bmm(self.attention, outp)
        # [bsz , hop, d_h*2]
        features = self.sentence_embedding
        ## capsule
        dropout_emb = self.drop(self.sentence_embedding)

        input_tiled = torch.unsqueeze(dropout_emb, -1).repeat(1, 1, 1, self.output_dim * self.output_atoms)
        votes = torch.sum(input_tiled * self.capsule_weights, dim=2)
        votes_reshaped = torch.reshape(votes, [-1, self.input_dim, self.output_dim, self.output_atoms])
        input_shape = self.sentence_embedding.shape
        logit_shape = np.stack([input_shape[0], self.input_dim, self.output_dim])

        self.activation, self.weights_b, self.weights_c = self.routing(votes = votes_reshaped,
                                                                               logit_shape=logit_shape,
                                                                               num_dims=4)
        self.logits = self.get_logits()
        self.votes = votes_reshaped
        return features

    def get_logits(self):
        logits = torch.norm(self.activation, dim=-1)
        return logits

    def routing(self, votes, logit_shape, num_dims):
        votes_t_shape = [3, 0, 1, 2]
        for i in range(num_dims - 4):
            votes_t_shape += [i + 4]
        r_t_shape = [1, 2, 3, 0]
        for i in range(num_dims - 4):
            r_t_shape += [i + 4]

        votes_trans = votes.permute(votes_t_shape)
        logits = nn.Parameter(torch.zeros(logit_shape[0], logit_shape[1], logit_shape[2]))
        activations = []

        # Iterative routing.
        for iteration in range(self.num_routing):
            route = F.softmax(logits, dim=2)
            preactivate_unrolled = route * votes_trans
            preact_trans = preactivate_unrolled.permute(r_t_shape)
            # delete bias to fit for unseen classes
            preactivate = torch.sum(preact_trans, dim=1)
            activation = self._squash(preactivate)
            activations.append(activation)
            # distances: [batch, input_dim, output_dim]
            act_3d = activation.unsqueeze(1)
            tile_shape = np.ones(num_dims, dtype=np.int32).tolist()
            tile_shape[1] = self.input_dim
            act_replicated = act_3d.repeat(tile_shape)
            distances = torch.sum(votes * act_replicated, dim=3)
            logits = logits + distances

        return activations[self.num_routing - 1], logits, route

    def _squash(self, input_tensor):
        norm = torch.norm(input_tensor, dim=2, keepdim= True)
        norm_squared = norm * norm
        return (input_tensor / norm) * (norm_squared / (0.5 + norm_squared))


    def init_weights(self):
        nn.init.xavier_uniform_(self.ws1.weight)
        nn.init.xavier_uniform_(self.ws2.weight)
        nn.init.xavier_uniform_(self.capsule_weights)

        self.ws1.weight.requires_grad_(True)
        self.ws2.weight.requires_grad_(True)
        self.capsule_weights.requires_grad_(True)

    def MT_Loss(self, lambda_, labels, raw_logits, seen_n, SUID_labels, margin=0.4,margin_SUID=0.49 ,downweight=0.5):
        """
        Multi-task Loss
        Args:
            lambda_ =  coefficient of the SUID loss in Multi-task loss 
            labels: tensor, one hot encoding of ground truth.
            raw_logits: tensor, model predictions in range [0, 1]
            seen_n: number of seen classes
            SUID_labels: tensor, one hot encoding of ground truth for SUID.
            margin: scalar, the margin after subtracting 0.5 from raw_logits.
            margin_SUID: scalar, the margin after subtracting 0.5 from raw_logits of SUID.
            downweight: scalar, the factor for negative cost.
        Returns:
            A tensor with cost for each data point of shape [batch_size].
        """
        
        
        seen_logits,unseen_logits = raw_logits.split(seen_n,1)
         
        seen_logits=torch.sum(seen_logits,dim=1,keepdim=True)/seen_n
        unseen_logits=torch.sum(unseen_logits,dim=1,keepdim=True)/(raw_logits.size()[1]-seen_n)
        self.SUID_logits = torch.cat([seen_logits,unseen_logits],1)
        SUID_logits = self.SUID_logits
        
        new_logits = SUID_logits - 0.5
        new_positive_cost = SUID_labels * (new_logits < margin_SUID).float() * ((new_logits - margin_SUID) ** 2)
        new_negative_cost = (1 - SUID_labels) * (new_logits > -margin_SUID).float() * ((new_logits + margin_SUID) ** 2)
        
        logits = raw_logits - 0.5
        positive_cost = labels * (logits < margin).float() * ((logits - margin) ** 2)
        negative_cost = (1 - labels) * (logits > -margin).float() * ((logits + margin) ** 2)
        return torch.cat([0.5 * positive_cost + downweight * 0.5 * negative_cost ,lambda_*(0.5 * new_positive_cost + downweight * 0.5 * new_negative_cost)],1)

    def compute_sim_with_SS(self, sc_intents, uc_intents):
        """
        get unseen and seen categories similarity by SIMILARITY SCORER(Cosine Distances)
        """
        sc_intents = torch.from_numpy(sc_intents)
        uc_intents = torch.from_numpy(uc_intents)
        feature_len = sc_intents.shape[1]
        sim = [self.cossim(xi.view(1,feature_len), yi.view(1,feature_len)) for xi in sc_intents for yi in uc_intents]
        
        sim = torch.stack(sim)
        
        sim = sim.view(len(sc_intents),len(uc_intents))
        #sim = self.softmax(sim)
        return sim.float() 

    def compute_sim_with_lable(self,  sc_intents, uc_intents, embedding):
        """
        get unseen and seen categories similarity by the embeddings of labels
        """
        if (embedding.nelement() != 0): 
            self.word_embedding = nn.Embedding.from_pretrained(embedding)
        sc_intents = self.word_embedding(sc_intents[0])
        uc_intents = self.word_embedding(uc_intents[0])

        sim = [self.cossim(xi.view(1,self.emb_len), yi.view(1,self.emb_len)) for xi in sc_intents for yi in uc_intents]
        sim = torch.stack(sim)
        sim = sim.view(len(sc_intents),len(uc_intents))
        sim = self.softmax(sim)
        return sim

    def cos_dis(self, I1,  I2):
        c = 1e-6 # prevent divid 0
        InnerPro = torch.sum(I1*I2,1,keepdim=True) # N,1,H,W
        len1 = torch.norm(I1, p=2,dim=1,keepdim=True) # ||x1||
        len2 = torch.norm(I2, p=2,dim=1,keepdim=True) # ||x2||

        divisor = len1*len2 # ||x1||*||x2||, N,1,H,W
        mask = torch.eq(divisor,0)
        divisor = divisor + (mask.float())*c # prevent divids 0
        cosA = torch.sum(InnerPro/divisor,1) # N,H,W
        return cosA
    
    def loss(self,alpha, label, seen_n, SUID_label):
        
        loss_val = self.MT_Loss(alpha, label, self.logits, seen_n, SUID_label)
        
        loss_val = torch.mean(loss_val)

        self_atten_mul = torch.matmul(self.attention, self.attention.permute([0, 2, 1])).float()
        sample_num, att_matrix_size, _ = self_atten_mul.shape
        self_atten_loss = (torch.norm(self_atten_mul - torch.from_numpy(np.identity(att_matrix_size)).float()).float()) ** 2

        return 1000 * loss_val + self.alpha * torch.mean(self_atten_loss)
