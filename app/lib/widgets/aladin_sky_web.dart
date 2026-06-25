// ignore: avoid_web_libraries_in_flutter
import 'dart:html' as html;
import 'dart:ui_web' as ui_web;

import 'package:flutter/widgets.dart';

const _viewType = 'ttn-aladin-sky';
bool _registered = false;

class AladinSky extends StatefulWidget {
  const AladinSky({super.key});

  @override
  State<AladinSky> createState() => _AladinSkyState();
}

class _AladinSkyState extends State<AladinSky> {
  @override
  void initState() {
    super.initState();
    if (!_registered) {
      _registered = true;
      ui_web.platformViewRegistry.registerViewFactory(_viewType, _buildElement);
    }
  }

  @override
  Widget build(BuildContext context) {
    return const HtmlElementView(viewType: _viewType);
  }
}

html.Element _buildElement(int id) {
  final container = html.DivElement()
    ..className = 'ttn-sky'
    ..style.cssText =
        'width:100%;height:100%;position:absolute;top:0;left:0;';

  final sky = html.DivElement()
    ..id = 'ttn-sky-$id'
    ..style.cssText = 'width:100%;height:100%;';

  container.append(sky);

  // Inject the init script — mirrors the pattern in tour.html
  html.document.head!.append(html.ScriptElement()..text = _initScript(id));

  return container;
}

String _initScript(int id) => '''
(function() {
  var divId = 'ttn-sky-$id';
  function init() {
    if (typeof A === 'undefined' || !A.init) { setTimeout(init, 200); return; }
    A.init.then(function() {
      var el = document.getElementById(divId);
      if (!el) return;
      var aladin = A.aladin('#' + divId, {
        survey: 'P/DSS2/color',
        fov: 65,
        cooFrame: 'ICRS',
        showReticle: false,
        showZoomControl: false,
        showFullscreenControl: false,
        showLayersControl: false,
        showGotoControl: false,
        showShareControl: false,
        showSimbadPointerControl: false,
        showCooGrid: false,
        showFrame: false,
        showContextMenu: false,
        showStatusBar: false,
        showProjectionControl: false,
        showCooGridControl: false,
      });
      // Start at a random position away from the galactic poles
      var ra  = Math.random() * 360;
      var dec = (Math.random() - 0.5) * 60;
      aladin.gotoRaDec(ra, dec);
      // Slow parallax drift — ~0.1 deg/sec along RA
      setInterval(function() {
        ra += 0.01;
        if (ra >= 360) ra -= 360;
        aladin.gotoRaDec(ra, dec);
      }, 100);
    }).catch(function() {});
  }
  init();
})();
''';
