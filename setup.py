setting = {
    'filepath': __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': None,
    'menu': {
        'uri': __package__,
        'name': '늑대닷컴 다운',
        'list': [
            {'uri': 'basic/setting', 'name': '설정'},
            {'uri': 'basic/manual',  'name': '수동 다운로드'},
            {'uri': 'basic/status',  'name': '진행 상황'},
            {'uri': 'basic/list',    'name': '다운로드 이력'},
            {'uri': 'log',           'name': '로그'},
        ],
    },
    'setting_menu': None,
    'default_route': 'normal',
}

from plugin import *

P = create_plugin_instance(setting)

try:
    from .mod_basic import ModuleBasic
    P.set_module_list([ModuleBasic])
except Exception as e:
    import traceback
    P.logger.error(f'Exception:{str(e)}')
    P.logger.error(traceback.format_exc())
