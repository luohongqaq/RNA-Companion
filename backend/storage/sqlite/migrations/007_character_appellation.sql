-- 角色表新增称呼列：不同角色对用户的称呼不同（名字/头衔/昵称），
-- 渲染角色卡模板时用它替换 {{ user_name }}。历史行先置空串，启动登记时再按角色清单填补。
ALTER TABLE characters ADD COLUMN user_appellation TEXT NOT NULL DEFAULT '';
