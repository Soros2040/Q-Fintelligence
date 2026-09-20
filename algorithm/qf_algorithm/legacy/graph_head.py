"""Shared one/two-layer graph regression head with explicit NumPy gradients."""
from __future__ import annotations
import numpy as np


class GraphHead:
    def __init__(self,input_dim,width,layers,l2,seed):
        rng=np.random.default_rng(seed);self.layers=layers;self.l2=l2
        self.parameters={'W1':rng.normal(0,1/np.sqrt(input_dim),(input_dim,width)),'b1':np.zeros(width),
            'Wo':rng.normal(0,1/np.sqrt(width),(width,1)),'bo':np.zeros(1)}
        if layers==2:self.parameters.update(W2=rng.normal(0,1/np.sqrt(width),(width,width)),b2=np.zeros(width))
        self.seed=seed

    def forward(self,x,graph):
        p=self.parameters;ax=graph@x;h1=np.tanh(ax@p['W1']+p['b1'])
        if self.layers==2:
            ah=graph@h1;h2=np.tanh(ah@p['W2']+p['b2'])
        else:ah=None;h2=h1
        prediction=(h2@p['Wo']+p['bo'])[...,0]
        return prediction,(ax,h1,ah,h2)

    def loss_gradient(self,x,graph,y,mask):
        p=self.parameters;prediction,(ax,h1,ah,h2)=self.forward(x,graph)
        error=np.where(mask,prediction-y,0);count=max(int(mask.sum()),1)
        loss=float(np.sum(error**2)/count)
        dy=(2*error/count)[...,None]
        gradient={'Wo':h2.reshape(-1,h2.shape[-1]).T@dy.reshape(-1,1),'bo':dy.sum(axis=(0,1))}
        dh2=dy@p['Wo'].T
        if self.layers==2:
            dz2=dh2*(1-h2*h2)
            gradient['W2']=ah.reshape(-1,ah.shape[-1]).T@dz2.reshape(-1,dz2.shape[-1])
            gradient['b2']=dz2.sum(axis=(0,1))
            dh1=np.swapaxes(graph,-1,-2)@(dz2@p['W2'].T)
        else:dh1=dh2
        dz1=dh1*(1-h1*h1)
        gradient['W1']=ax.reshape(-1,ax.shape[-1]).T@dz1.reshape(-1,dz1.shape[-1])
        gradient['b1']=dz1.sum(axis=(0,1))
        for key in p:
            if key.startswith('W'):
                loss+=self.l2*float(np.sum(p[key]**2))
                gradient[key]+=2*self.l2*p[key]
        return loss,gradient

    def fit(self,x,graph,y,mask,epochs=40):
        available=x[mask]
        self.xmean=available.mean(axis=0);self.xscale=np.maximum(available.std(axis=0),1e-8)
        self.ymean=float(y[mask].mean());self.yscale=max(float(y[mask].std()),1e-8)
        x=(x-self.xmean)/self.xscale;y=np.where(mask,(y-self.ymean)/self.yscale,0)
        m={k:np.zeros_like(v) for k,v in self.parameters.items()};v={k:np.zeros_like(v) for k,v in self.parameters.items()}
        rng=np.random.default_rng(self.seed);step=0;history=[]
        for epoch in range(epochs):
            order=rng.permutation(len(x));losses=[]
            for start in range(0,len(x),8):
                idx=order[start:start+8];loss,g=self.loss_gradient(x[idx],graph[idx],y[idx],mask[idx]);step+=1
                norm=np.sqrt(sum(np.sum(value**2) for value in g.values()))
                for key in g:
                    grad=g[key]/max(1.,norm/5)
                    m[key]=.9*m[key]+.1*grad;v[key]=.999*v[key]+.001*grad**2
                    self.parameters[key]-=.01*(m[key]/(1-.9**step))/(np.sqrt(v[key]/(1-.999**step))+1e-8)
                losses.append(loss)
            history.append(float(np.mean(losses)))
        return history

    def predict(self,x,graph):
        prediction=[]
        for start in range(0,len(x),16):
            normalized=(x[start:start+16]-self.xmean)/self.xscale
            values,_=self.forward(normalized,graph[start:start+16]);prediction.append(values*self.yscale+self.ymean)
        return np.concatenate(prediction)

    def export(self):
        return {**self.parameters,'xmean':self.xmean,'xscale':self.xscale,'ymean':np.array(self.ymean),'yscale':np.array(self.yscale)}
