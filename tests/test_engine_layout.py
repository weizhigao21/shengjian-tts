import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QLabel

from voice_workbench.gui import Window, configure_font


@pytest.mark.parametrize('width,height', [(1080,720), (1380,900)])
def test_engine_asset_labels_stay_inside_scroll_view_with_full_right_gutter(tmp_path, qtbot, qapp, width, height):
    qapp.setStyle('Fusion')
    configure_font(qapp)
    window = Window(8799,tmp_path,autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    panel = window.settings
    panel.timer.stop()
    window.resize(width,height)
    window.set_page(3)
    panel.set_section(4)
    panel.engine_details_toggle.setChecked(True)
    window.show()
    qtbot.wait(20)
    rects = []
    for status in ('available','downloading','verifying','paused','failed','done'):
        entry = next(e for e in panel.engine_catalog['backends'] if e['id']=='cuda')
        panel.engine_backend.setCurrentIndex(panel.engine_backend.findData(entry['id']))
        panel.model_data = {'engine':{'ready':True,'path':'C:/very-long-current-runtime-folder/audiocpp_server.exe',
            'options':[{'backend':entry['id'],'status':status,'archives':[
                {**a,'received':a['size'],'status':status} for a in entry['archives']]}]}}
        panel.render_engine()
        qtbot.wait(20)
        viewport = panel.engine_scroll.viewport()
        for row in panel.engine_asset_rows:
            for control in row:
                point = viewport.mapFromGlobal(control.mapToGlobal(QPoint()))
                assert point.x() >= 0
                assert point.x()+control.width() <= viewport.width()-12
                if isinstance(control,QLabel):
                    rendered = QLabel.text(control)
                    assert control.fontMetrics().horizontalAdvance(rendered) <= control.contentsRect().width()
                    assert control.contentsRect().height() >= control.fontMetrics().height()
        for control in (panel.runtime_label,panel.engine_description,panel.engine_more):
            point = viewport.mapFromGlobal(control.mapToGlobal(QPoint()))
            assert point.x()+control.width() <= viewport.width()-12
        action = panel.engine_download
        rects.append((action.mapTo(window,QPoint()).x(),action.mapTo(window,QPoint()).y(),action.width(),action.height()))
    assert len(set(rects)) == 1
    assert str(tmp_path) in panel.data_location.text()
    window.close()
