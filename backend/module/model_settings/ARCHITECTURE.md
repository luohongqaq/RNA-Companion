# 模型设置架构

## 总览

```mermaid
flowchart LR
    A[API 模型设置接口] --> S[ModelSettingsService]
    S --> P[ModelSettingsRepository 端口]
    P --> D[DotenvModelSettingsRepository]
    D --> F[.env 保存值]
    D --> E[Settings 与环境变量<br/>计算有效值和重启提示]
```

[ModelSettingsService](core/processing/service.py)校验请求，[DotenvModelSettingsRepository](repository/dotenv.py)实现读取和保存；[api/container.py](../../api/container.py)负责装配。

## 查询和修改

```mermaid
flowchart TD
    A[选择 reply 或 default] --> B[读取 .env 与有效设置]
    B --> C[返回保存值、有效值、覆盖字段和版本]
    C --> D[用户提交变更与版本]
    D --> E[校验字段及模型地址]
    E --> F[SQLite 写事务协调版本比较]
    F --> G{版本一致?}
    G -->|否| X[返回冲突]
    G -->|是| H[写临时文件并原子替换 .env]
    H --> I[重新读取并返回设置]
```

未提交字段不修改，密钥只返回是否配置；重启提示根据有效设置与应用启动时使用的设置比较。保存不等于热替换已创建的客户端。数据类型见 [DOMAIN.md](DOMAIN.md)。
