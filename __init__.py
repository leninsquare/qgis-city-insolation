def classFactory(iface):
    from .module_insolation import ModuleInsolationPlugin

    return ModuleInsolationPlugin(iface)
