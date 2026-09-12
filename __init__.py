def classFactory(iface):
    from .arte import ArmaExportPlugin
    return ArmaExportPlugin(iface)