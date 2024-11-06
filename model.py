import numpy as np
import torch
from torch import nn

class MLP(nn.Module):
    def __init__(self, depth=3, n_input=1, n_units=512):
        super(MLP, self).__init__()
        layers = []
        for i in range(depth):
            if i == 0:
                layers.append(nn.Linear(n_input, n_units))
            else:
                layers.append(nn.Linear(n_units, n_units))
            layers.append(nn.LayerNorm(normalized_shape=n_units))
            layers.append(nn.LeakyReLU())
        self.layers = nn.Sequential(*layers)
        
    def forward(self, x):
        return self.layers(x)
    
    def get_num_parameters(self):
        total = 0
        for p in self.parameters():
            total += np.prod(p.shape)
        return total
            
def modified_sigmoid(x):
    return 2*torch.sigmoid(x)**np.log(10) + 1e-7
        
        
class DDSPDecoder(nn.Module):
    def __init__(self, mlp_depth, n_units, n_harmonics, n_bands, hop_length, sr, reverb_len, data):
        """
        Parameters
        ----------
        mlp_depth: int
            Depth of each multilayer perceptron
        n_units: int
            Number of units in each multilayer perceptron
        n_harmonics: int
            Number of harmonic amplitudes to output
        n_bands: int
            Number of bands to use in the noise filter
        hop_length: int
            Hop length to use when doing style transfer
        sr: int
            Sample rate
        reverb_len: int
            Number of samples for reverb signal
        data: InstrumentData
            Dataset that this is trained on; will be used to save off loudness_mu
            and loudness_std for style transfer
        """
        super(DDSPDecoder, self).__init__()
        self.FMLP = MLP(mlp_depth, 1, n_units)
        self.LMLP = MLP(mlp_depth, 1, n_units)
        
        self.gru = nn.GRU(input_size=n_units*2, hidden_size=n_units, num_layers=1, bias=True, batch_first=True)
        self.FinalMLP = MLP(mlp_depth, n_units*3, n_units)
        self.HarmonicsDecoder = nn.Linear(n_units, n_harmonics)
        self.AmplitudeDecoder = nn.Linear(n_units, 1)
        self.FFTDecoder = nn.Linear(n_units, n_bands)
        self.reverb = nn.Parameter(torch.rand(reverb_len)*1e-4-0.5e-4)
        self.n_harmonics = n_harmonics
        self.n_bands = n_bands
        self.hop_length = hop_length
        self.sr = sr
        self.loudness_mu = data.loudness_mu
        self.loudness_std = data.loudness_std
    
    def forward(self, F, L, respect_nyquist=False):
        FOut = self.FMLP(F)
        LOut = self.LMLP(L)
        FL = torch.concatenate((FOut, LOut), axis=2)
        G = self.gru(FL)[0]
        G = torch.concatenate((FOut, LOut, G), axis=2)
        final = self.FinalMLP(G)
        S = modified_sigmoid(self.FFTDecoder(final))
        A = modified_sigmoid(self.AmplitudeDecoder(final))
        C = modified_sigmoid(self.HarmonicsDecoder(final))
        # Zero out amplitudes above the nyquist rate
        if respect_nyquist:
            FMul = torch.arange(1, self.n_harmonics+1).view(1, 1, self.n_harmonics).to(C.device)
            CFreqs = F*FMul
            C[CFreqs >= self.sr//2] = 0
        C = C/(1e-8+torch.sum(C, axis=2, keepdims=True))
        return A, C, S, torch.tanh(self.reverb)
    
    def get_num_parameters(self):
        total = 0
        for p in self.parameters():
            total += np.prod(p.shape)
        return total
    
    def style_transfer(self, x, device, pitch_shift=0):
        from synthesis import synthesize
        from torchcrepe import predict
        from utils import extract_loudness
        x = x*0.5/np.max(np.abs(x))
        pitch = predict(torch.from_numpy(x).view((1, x.size)),self.sr,self.hop_length,50,2000,'full',
                                batch_size=2048,device=device).flatten()
        pitch *= 2**(pitch_shift/12)
        loudness = extract_loudness(x, self.sr, self.hop_length)
        loudness = (loudness-self.loudness_mu)/self.loudness_std
        
        N = len(x)
        X = torch.from_numpy(x)
        X = X.view(1, N, 1).to(device)

        N = min(len(loudness), len(pitch))
        loudness = loudness[0:N]
        pitch = pitch[0:N]
        L = torch.from_numpy(loudness)
        L = L.view(1, N, 1)
        L = L.to(device)
        F = pitch.view(1, N, 1)
        F = F.to(device)
        
        A, C, P, reverb = self.forward(F, L)
        y = synthesize(A, C, F/2, P, self.hop_length, self.sr, reverb)
        return y.detach().cpu().numpy().flatten()