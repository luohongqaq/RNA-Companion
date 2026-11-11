"""可选CUDA批量入库编码器；与BGE-M3默认CLS归一化的dense表示一致。"""
class CudaSceneEncoder:
    def __init__(self, path):
        import torch
        from transformers import AutoModel, AutoTokenizer
        if not torch.cuda.is_available():
            raise ValueError("--cuda需要支持CUDA的Python环境")
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
        self.model = AutoModel.from_pretrained(str(path), local_files_only=True,
            trust_remote_code=False, torch_dtype=torch.float16).to("cuda").eval()

    def encode(self, texts, *, batch_size=16, max_length=384, **kwargs):
        import numpy as np
        output = []
        for start in range(0, len(texts), batch_size):
            tokens = self.tokenizer(texts[start:start+batch_size], padding=True, truncation=True,
                                    max_length=max_length, return_tensors="pt").to("cuda")
            with self.torch.inference_mode():
                vector = self.model(**tokens).last_hidden_state[:, 0]
                vector = self.torch.nn.functional.normalize(vector, p=2, dim=1)
            output.append(vector.float().cpu().numpy())
        return {"dense_vecs": np.concatenate(output)}
