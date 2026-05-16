# Hermes 模型配置指南

## 当前配置的模型

### 1. GLM 模型（默认可用）
- **模型**: `glm-4.5-flash`
- **提供商**: GLM (智谱AI)
- **状态**: ✅ 可用
- **特点**: 中文支持好，响应速度快

### 2. OpenRouter 模型（需要充值）
- **模型列表**:
  - `deepseek/deepseek-chat`
  - `qwen/qwen-2.5-72b-instruct`
  - `meta-llama/llama-3.1-8b-instruct`
- **提供商**: OpenRouter
- **状态**: ⚠️ 需要充值
- **充值地址**: https://openrouter.ai/settings/credits

## 模型切换方法

### 方法1：使用命令行切换
```bash
# 切换到 GLM 模型
hermes config set model.provider glm
hermes config set model.default glm-4.5-flash

# 切换到 OpenRouter 自动模式
hermes config set model.provider auto
hermes config set model.default ""
```

### 方法2：直接编辑配置文件
```bash
hermes config edit
```

### 方法3：在 Hermes Desktop 中选择
1. 打开 Hermes Desktop
2. 查看模型设置
3. 选择可用的模型

## 模型特点对比

| 模型 | 语言能力 | 推理能力 | 速度 | 中文支持 |
|------|----------|----------|------|----------|
| GLM-4.5-Flash | 优秀 | 良好 | 快 | 优秀 |
| DeepSeek Chat | 优秀 | 优秀 | 中等 | 优秀 |
| Qwen 2.5 72B | 优秀 | 优秀 | 慢 | 优秀 |
| Llama 3.1 8B | 良好 | 良好 | 快 | 一般 |

## 故障排除

### 问题1：OpenRouter 模型无法使用
**症状**: 显示 402 错误（Insufficient credits）
**解决**: 
1. 访问 https://openrouter.ai/settings/credits
2. 购买积分
3. 重启 Hermes Desktop

### 问题2：模型连接失败
**解决**:
1. 检查网络连接
2. 验证 API 密钥
3. 重启 Hermes Desktop
4. 切换到 GLM 模型作为备选

### 问题3：响应速度慢
**建议**:
1. 使用 GLM-4.5-Flash（速度快）
2. 避免使用大型模型（如 Qwen 2.5 72B）
3. 检查网络延迟

## 自动切换机制

当前配置支持自动切换：
- 优先尝试 OpenRouter 模型
- 如果失败，自动切换到 GLM 模型
- 确保始终有可用的模型

## 更新模型列表

如需添加新的 OpenRouter 模型：
```bash
hermes config set providers.openrouter.models "['model1', 'model2', 'model3']"
```

## 注意事项

1. **OpenRouter 需要充值**：首次使用需要购买积分
2. **网络要求**：OpenRouter 需要稳定的网络连接
3. **成本控制**：大型模型（如 72B）调用成本较高
4. **备选方案**：GLM 模型始终可用作为备选

---
创建时间：2026-05-16 03:25
更新时间：2026-05-16 03:25