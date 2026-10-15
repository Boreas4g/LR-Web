export type LocalUser = {
  name: string;
  role: string;
  username: string;
};

// 交付态：单机单人操作、无登录。后端在 LR_AUTH_ENABLED=false（默认）时把
// 所有请求视为内置操作员；若日后重新开启认证，需要恢复登录页与 token 逻辑。
const OPERATOR_USER: LocalUser = {
  username: 'operator',
  name: '操作员',
  role: '系统管理员',
};

export function getCurrentUser(): LocalUser {
  return OPERATOR_USER;
}
