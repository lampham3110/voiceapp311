from __future__ import absolute_import

import collections
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from re import search

import arcgis.features
import arcgis.gis
import arcgis.env
from warnings import warn
from arcgis._impl.common._mixins import PropertyMap
from arcgis._impl.common._utils import _date_handler
from arcgis.geometry import SpatialReference, Polygon
from arcgis.gis import Layer, _GISResource, Item

from uuid import uuid4 #unique ids for layers in web map
import datetime
_log = logging.getLogger(__name__)


@contextmanager
def _tempinput(data):
    temp = tempfile.NamedTemporaryFile(delete=False)
    temp.write((bytes(data, 'UTF-8')))
    temp.close()
    yield temp.name
    os.unlink(temp.name)


class SceneLayer(Layer):
    """
    The SceneSerice is represents a 3D service published on server.
    """
    def __init__(self, url, gis=None):
        """
        Constructs a feature layer given a feature layer URL
        :param url: feature layer url
        :param gis: optional, the GIS that this layer belongs to. Required for secure feature layers.
        """
        super(SceneLayer, self).__init__(url, gis)


class WebMap(collections.OrderedDict):
    """
    Represents a web map item and provides access to its basemaps and operational layers as well
    as functionality to visualize and interact with them.
    http://resources.arcgis.com/en/help/arcgis-web-map-json/index.html#/Web_map_format_overview/02qt00000007000000/
    """

    def __init__(self, webmapitem=None):
        """
        Constructs an empty WebMap object. If an web map Item is passed, constructs a WebMap object from item on
        ArcGIS Online or Enterprise.
        """
        if webmapitem:
            if webmapitem.type.lower() != 'web map':
                raise TypeError("item type must be web map")
            self.item = webmapitem
            self._gis = webmapitem._gis
            self._con = self._gis._con
            self._webmapdict = self.item.get_data()
            pmap = PropertyMap(self._webmapdict)
            self.definition = pmap
            self._layers = None
            self._basemap = None
            self._extent = self.item.extent

        else:
            #default spatial ref for current web map
            self._default_spatial_reference = {'wkid': 4326,
                                               'latestWkid': 4326}

            #pump in a simple, default webmap dict - no layers yet, just basemap
            self._basemap = {
                            'baseMapLayers':[{'id':'defaultBasemap',
                                              'layerType':'ArcGISTiledMapServiceLayer',
                                              'url':'https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer',
                                              'visibility':True,
                                              'opacity':1,
                                              'title':'World Topographic Map'
                                              }],
                            'title':'Topographic'
                            }
            self._webmapdict = {'baseMap':self._basemap,
            'spatialReference':self._default_spatial_reference,
            'version':'2.10',
            'authoringApp': 'ArcGISPythonAPI',
            'authoringAppVersion': str(arcgis.__version__),
            }
            pmap = PropertyMap(self._webmapdict)
            self.definition = pmap
            self._gis = arcgis.env.active_gis
            if self._gis: #you can also have a case where there is no GIS obj
                self._con = self._gis._con
            else:
                self._con = None
            self.item = None
            self._layers = []
            self._extent = []

    # def _repr_html_(self):
    def _ipython_display_(self, **kwargs):
        from arcgis.widgets import MapView
        # return '<iframe width=960 height=600 src="'+self.item._portal.url  + "/home/webmap/viewer.html?webmap=" + self.item.itemid + '"/>'
        mapwidget = MapView(gis=self._gis, item=self.item)
        return mapwidget._ipython_display_(**kwargs)

    def __repr__(self):
        return 'WebMap at ' + self.item._portal.url  + "/home/webmap/viewer.html?webmap=" + self.item.itemid

    def __str__(self):
        return json.dumps(self, default=_date_handler)

    def add_layer(self, layer, options=None):
        """
        Adds the given layer to the WebMap.

        ==================     ====================================================================
        **Argument**           **Description**
        ------------------     --------------------------------------------------------------------
        layer                  Required object. You can add any Layer objects such as FeatureLayer, MapImageLayer,
                               ImageryLayer etc. You can also add Item objects and FeatureSet and FeatureCollections.
        ------------------     --------------------------------------------------------------------
        options                Optional dict. Specify properties such as title, symbol, opacity, visibility, renderer
                               for the layer that is added. If not specified, appropriate defaults are applied.
        ==================     ====================================================================

        .. code-block:: python  (optional)

           USAGE EXAMPLE: Add feature layer and map image layer item objects to the WebMap object.

           crime_fl_item = gis.content.search("2012 crime")[0]
           streets_item = gis.content.search("LA Streets","Map Service")[0]

           wm = WebMap()  # create an empty web map with a default basemap
           wm.add_layer(streets_item)
           wm.add_layer(fl_item, {'title':'2012 crime in LA city',
                                  'opacity':0.5,
                                  'visibility':False})

        :return:
            True if layer was successfully added. Else, raises appropriate exception.
        """
        if options is None:
            options = {}
        #region extact basic info from options
        title = options['title'] if options and 'title' in options else None
        opacity = options['opacity'] if options and 'opacity' in options else 1
        visibility = options['visibility'] if options and 'visibility' in options else True
        layer_spatial_ref = options['spatialReference'] if options and 'spatialReference' in options else None
        popup = options['popup'] if options and 'popup' in options else None  # from draw method
        item_id = None
        #endregion

        #region extract rendering info from options
        # info for feature layers
        definition_expression = options['definition_expression'] if options and 'definition_expression' in options else None
        renderer = options['renderer'] if options and 'renderer' in options else None
        renderer_field = options['field_name'] if options and 'field_name' in options else None
        self._extent = options['extent'] if options and 'extent' in options else self._extent #from map widget
        fset_symbol = options['symbol'] if options and 'symbol' in options else None  # from draw method

        # info for raster layers
        image_service_parameters = options['imageServiceParameters'] \
            if options and 'imageServiceParameters' in options else None

        #endregion

        #region infer layer type
        layer_type = None
        if isinstance(layer, Layer) or isinstance(layer, arcgis.features.FeatureSet):
            if hasattr(layer, 'properties'):
                if hasattr(layer.properties, 'name'):
                    title = layer.properties.name if title is None else title

                #find layer type
                if isinstance(layer, arcgis.features.FeatureLayer) or isinstance(layer, arcgis.features.FeatureCollection) \
                        or isinstance(layer, arcgis.features.FeatureSet):
                    layer_type = 'ArcGISFeatureLayer'
                elif isinstance(layer, arcgis.raster.ImageryLayer):
                    layer_type='ArcGISImageServiceLayer'
                    #todo : get renderer info

                elif isinstance(layer, arcgis.mapping.MapImageLayer):
                    layer_type='ArcGISMapServiceLayer'
                elif isinstance(layer, arcgis.mapping.VectorTileLayer):
                    layer_type='VectorTileLayer'
                elif isinstance(layer, arcgis.realtime.StreamLayer):
                    layer_type='ArcGISStreamLayer'

                if hasattr(layer.properties, 'serviceItemId'):
                    item_id = layer.properties.serviceItemId
            elif isinstance(layer, arcgis.features.FeatureSet):
                layer_type = 'ArcGISFeatureLayer'
        elif isinstance(layer, arcgis.gis.Item):
            #set the item's extent
            if not self._extent:
                self._extent = layer.extent
            if hasattr(layer, 'layers'):
                if layer.type == 'Feature Collection':
                    options['serviceItemId'] = layer.itemid

                for lyr in layer.layers:  # recurse - works for all.
                    self.add_layer(lyr, options)
                return True  # end add_layer execution after iterating through each layer.
            else:
                raise TypeError('Item object without layers is not supported')
        elif isinstance(layer, arcgis.features.FeatureLayerCollection):
            if not self._extent:
                if hasattr(layer.properties, 'fullExtent'):
                    self._extent = layer.properties.fullExtent
            if hasattr(layer, 'layers'):
                for lyr in layer.layers:  # recurse
                    self.add_layer(lyr, options)
                return True
            else:
                raise TypeError('FeatureLayerCollection object without layers is not supported')
        else:
            raise TypeError("Input layer should either be a Layer object or an Item object. To know the supported layer types, refer" +
                                'to https://developers.arcgis.com/web-map-specification/objects/operationalLayers/')
        #endregion

        # region create the new layer dict in memory
        new_layer = {'title':title,
                     'opacity':opacity,
                     'visibility':visibility,
                     'id':uuid4().__str__()}

        # if renderer info is available, then write layer definition
        layer_definition = {'definitionExpression':definition_expression}

        if renderer:
            layer_definition['drawingInfo'] = {'renderer':renderer}
        new_layer['layerDefinition'] = layer_definition

        if layer_type:
            new_layer['layerType'] = layer_type

        if item_id:
            new_layer['itemId'] = item_id

        if hasattr(layer, 'url'):
            new_layer['url'] = layer.url
        elif isinstance(layer, arcgis.features.FeatureCollection):  # feature collection item on web GIS
            if 'serviceItemId' in options:
                # if ItemId is found, then type is fc and insert item id. Else, leave the type as ArcGISFeatureLayer
                new_layer['type'] = "Feature Collection"
                new_layer['itemId'] = options['serviceItemId']
            elif hasattr(layer, 'properties'):
                if hasattr(layer.properties, 'layerDefinition'):
                    if hasattr(layer.properties.layerDefinition, 'serviceItemId'):
                        new_layer['type'] = 'Feature Collection'   # if ItemId is found, then type is fc and insert item id
                        new_layer['itemId'] = layer.properties.layerDefinition.serviceItemId
                elif hasattr(layer, "layer"):
                    if hasattr(layer.layer, "layers"):
                        if hasattr(layer.layer.layers[0], "layerDefinition"):
                            if hasattr(layer.layer.layers[0].layerDefinition, 'serviceItemId'):
                                new_layer['type'] = 'Feature Collection'  # if ItemId is found, then type is fc and insert item id
                                new_layer['itemId'] = layer.layer.layers[0].layerDefinition.serviceItemId

        if layer_type == 'ArcGISImageServiceLayer':
            #find if raster functions are available
            if 'options' in layer._lyr_json:
                if isinstance(layer._lyr_json['options'], str): #sometimes the rendering info is a string
                    #load json
                    layer_options = json.loads(layer._lyr_json['options'])
                else:
                    layer_options = layer._lyr_json['options']

                if 'imageServiceParameters' in layer_options:
                    #get renderingRule and mosaicRule
                    new_layer.update(layer_options['imageServiceParameters'])

            #if custom rendering rule is passed, then overwrite this
            if image_service_parameters:
                new_layer['renderingRule'] = image_service_parameters['renderingRule']

        # inmem FeatureCollection
        if isinstance(layer, arcgis.features.FeatureCollection):
            if hasattr(layer, "layer"):
                if hasattr(layer.layer, "layers"):
                    fc_layer_definition = dict(layer.layer.layers[0].layerDefinition)
                    fc_feature_set = dict(layer.layer.layers[0].featureSet)
                else:
                    fc_layer_definition = dict(layer.layer.layerDefinition)
                    fc_feature_set = dict(layer.layer.featureSet)
            else:
                fc_layer_definition = dict(layer.properties.layerDefinition)
                fc_feature_set = dict(layer.properties.featureSet)

            if 'title' not in fc_layer_definition:
                fc_layer_definition['title'] = title

            new_layer['featureCollection'] = {'layers':
                                              [{'featureSet': fc_feature_set,
                                                'layerDefinition': fc_layer_definition}
                                               ]}

        # inmem FeatureSets - typically those which users pass to the `MapView.draw()` method
        if isinstance(layer, arcgis.features.FeatureSet):
            if not layer_spatial_ref:
                if hasattr(layer, 'spatial_reference'):
                    layer_spatial_ref = layer.spatial_reference
                else:
                    layer_spatial_ref = self._default_spatial_reference

            if 'spatialReference' not in layer.features[0].geometry: # webmap seems to need spatialref for each geometry
                for feature in layer:
                    feature.geometry['spatialReference'] = layer_spatial_ref

            fset_dict = layer.to_dict()
            fc_layer_definition = {'geometryType':fset_dict['geometryType'],
                                   'fields':fset_dict['fields'],
                                   'objectIdField':layer.object_id_field_name,
                                   'type':'Feature Layer',
                                   'spatialReference':layer_spatial_ref,
                                   'name':title}

            #region set up default symbols if one is not available.
            if not fset_symbol:
                if fc_layer_definition['geometryType'] == 'esriGeometryPolyline':
                    fset_symbol = {"color":[0,0,0,255],
                                   "width": 1.33,
                                   "type": "esriSLS",
                                   "style": "esriSLSSolid"}
                elif fc_layer_definition['geometryType'] in ['esriGeometryPolygon','esriGeometryEnvelope']:
                    fset_symbol={"color": [0,0,0,64],
                                 "outline": {
                                     "color": [0,0,0,255],
                                     "width": 1.33,
                                     "type": "esriSLS",
                                     "style": "esriSLSSolid"},
                                 "type": "esriSFS",
                                 "style": "esriSFSSolid"}
                elif fc_layer_definition['geometryType'] in ['esriGeometryPoint', 'esriGeometryMultipoint']:
                    fset_symbol={"angle": 0,
                                 "xoffset": 0,
                                 "yoffset": 12,
                                 "type": "esriPMS",
                                 "url": "http://esri.github.io/arcgis-python-api/notebooks/nbimages/pink.png",
                                 "contentType": "image/png",
                                 "width": 24,
                                 "height": 24}
            #endregion

            #insert symbol into the layerDefinition of featureCollection - pro style
            if renderer:
                fc_layer_definition['drawingInfo'] = {'renderer':renderer}
            else: #use simple, default renderer
                fc_layer_definition['drawingInfo'] = {'renderer': {
                    'type':'simple',
                    'symbol':fset_symbol
                    }
                }

            new_layer['featureCollection'] = {
                'layers':[
                    {'featureSet':{'geometryType':fset_dict['geometryType'],
                                   'features':fset_dict['features']},
                     'layerDefinition':fc_layer_definition
                }]
            }
        #endregion

        # region Process popup info
        if layer_type in ['ArcGISFeatureLayer', 'ArcGISImageServiceLayer', 'Feature Collection']:  # supports popup
            popup = {'title': title,
                     'fieldInfos': [],
                     'description': None,
                     'showAttachments': True,
                     'mediaInfos': []}

            fields_list = []
            if isinstance(layer, arcgis.features.FeatureLayer) or isinstance(layer, arcgis.raster.ImageryLayer):
                if hasattr(layer.properties, 'fields'):
                    fields_list = layer.properties.fields
            elif isinstance(layer, arcgis.features.FeatureSet):
                if hasattr(layer, 'fields'):
                    fields_list = layer.fields
            elif isinstance(layer, arcgis.features.FeatureCollection):
                if hasattr(layer.properties, 'layerDefinition'):
                    if hasattr(layer.properties.layerDefinition, 'fields'):
                        fields_list = layer.properties.layerDefinition.fields

            for f in fields_list:
                if isinstance(f, dict) or isinstance(f, PropertyMap):
                    field_dict = {'fieldName': f['name'],
                                  'label': f['alias'] if 'alias' in f else f['name'],
                                  'isEditable': f['editable'] if 'editable' in f else True,
                                  'visible': True}
                elif isinstance(f, str):  # some layers are saved with fields that are just a list of strings
                    field_dict = {'fieldName': f,
                                  'label': f,
                                  'isEditable': True,
                                  'visible': True}
                if field_dict:
                    popup['fieldInfos'].append(field_dict)
        else:
            popup = None

        if popup:
            if isinstance(layer, arcgis.features.FeatureLayer) or isinstance(layer, arcgis.raster.ImageryLayer):
                new_layer['popupInfo'] = popup
            elif isinstance(layer, arcgis.features.FeatureSet) or isinstance(layer, arcgis.features.FeatureCollection):
                new_layer['featureCollection']['layers'][0]['popupInfo'] = popup

        # endregion

        # region add layers to operationalLayers
        if 'operationalLayers' not in self._webmapdict.keys():
            # there no layers yet, create one here
            self._webmapdict['operationalLayers'] = [new_layer]
            self.definition = PropertyMap(self._webmapdict)
        else:
            # there are operational layers, just append to it
            self._webmapdict['operationalLayers'].append(new_layer)
            self.definition = (PropertyMap(self._webmapdict))
        # endregion

        # update layers
        self._layers.append(PropertyMap(new_layer))
        return True

    def _process_extent(self):
        """
        internal method to transform extent to a string of xmin, ymin, xmax, ymax
        If extent is not in wgs84, it projects
        :return:
        """
        if isinstance(self._extent, list):
            #passed from Item's extent flatten the extent. Item's extent is always in 4326, no need to project
            extent_list = [element for sublist in self._extent for element in sublist]

            #convert to string
            return ','.join(str(e) for e in extent_list)

        elif isinstance(self._extent, dict):
            #passed from MapView.extent
            if 'spatialReference' in self._extent:
                if 'latestWkid' in self._extent['spatialReference']:
                    if self._extent['spatialReference']['latestWkid'] != 4326:
                        #use geometry service to project
                        input_geom = [{'x':self._extent['xmin'], 'y':self._extent['ymin']},
                                      {'x':self._extent['xmax'], 'y':self._extent['ymax']}]

                        result = arcgis.geometry.project(input_geom,
                                                         in_sr=self._extent['spatialReference']['latestWkid'],
                                                         out_sr=4326)

                        #process and return the result
                        e = [result[0]['x'],result[0]['y'],result[1]['x'],result[1]['y']]
                        return ','.join(str(i) for i in e)

            #case when there is no spatialReference. Then simply extract the extent
            if 'xmin' in self._extent:
                e = self._extent
                e= [e['xmin'], e['ymin'],e['xmax'],e['ymax']]
                return ','.join(str(i) for i in e)

        #if I don't know how to process the extent.
        return self._extent

    def save(self, item_properties, thumbnail=None, metadata=None, owner=None, folder=None):
        """
        Save the WebMap object into a new web map Item in your GIS.

        .. note::
            If you started out with a fresh WebMap object, use this method to save it as a the web map item in your GIS.

            If you started with a WebMap object from an existing web map item, calling this method will create a new item
            with your changes. If you want to update the existing web map item with your changes, call the `update()`
            method instead.

        ===============     ====================================================================
        **Argument**        **Description**
        ---------------     --------------------------------------------------------------------
        item_properties     Required dictionary. See table below for the keys and values.
        ---------------     --------------------------------------------------------------------
        thumbnail           Optional string. Either a path or URL to a thumbnail image.
        ---------------     --------------------------------------------------------------------
        metadata            Optional string. Either a path or URL to the metadata.
        ---------------     --------------------------------------------------------------------
        owner               Optional string. Defaults to the logged in user.
        ---------------     --------------------------------------------------------------------
        folder              Optional string. Name of the folder where placing item.
        ===============     ====================================================================

        *Key:Value Dictionary Options for Argument item_properties*

        =================  =====================================================================
        **Key**            **Value**
        -----------------  ---------------------------------------------------------------------
        typeKeywords       Optional string. Provide a lists all sub-types, see URL 1 below for valid values.
        -----------------  ---------------------------------------------------------------------
        description        Optional string. Description of the item.
        -----------------  ---------------------------------------------------------------------
        title              Optional string. Name label of the item.
        -----------------  ---------------------------------------------------------------------
        tags               Optional string. Tags listed as comma-separated values, or a list of strings.
                           Used for searches on items.
        -----------------  ---------------------------------------------------------------------
        snippet            Optional string. Provide a short summary (limit to max 250 characters) of the what the item is.
        -----------------  ---------------------------------------------------------------------
        accessInformation  Optional string. Information on the source of the content.
        -----------------  ---------------------------------------------------------------------
        licenseInfo        Optional string.  Any license information or restrictions regarding the content.
        -----------------  ---------------------------------------------------------------------
        culture            Optional string. Locale, country and language information.
        -----------------  ---------------------------------------------------------------------
        access             Optional string. Valid values are private, shared, org, or public.
        -----------------  ---------------------------------------------------------------------
        commentsEnabled    Optional boolean. Default is true, controls whether comments are allowed (true)
                           or not allowed (false).
        -----------------  ---------------------------------------------------------------------
        culture            Optional string. Language and country information.
        =================  =====================================================================

        URL 1: http://resources.arcgis.com/en/help/arcgis-rest-api/index.html#//02r3000000ms000000

        :return:
            Item object corresponding to the new web map Item created.
        """

        item_properties['type'] = 'Web Map'
        item_properties['extent'] = self._process_extent()
        item_properties['text'] = json.dumps(self._webmapdict, default=_date_handler)

        if 'title' not in item_properties or 'snippet' not in item_properties or 'tags' not in item_properties:
            raise RuntimeError("title, snippet and tags are required in item_properties dictionary")

        new_item = self._gis.content.add(item_properties, thumbnail=thumbnail, metadata=metadata, owner=owner,
                                         folder=folder)
        if not hasattr(self, 'item'):
            self.item = new_item

        return new_item

    def update(self, item_properties=None, thumbnail=None, metadata=None):
        """
        Updates the web map item in your GIS with the changes you made to the WebMap object. In addition, you can update
        other item properties, thumbnail and metadata.

        .. note::
            If you started with a WebMap object from an existing web map item, calling this method will update the item
            with your changes.

            If you started out with a fresh WebMap object (without a web map item), calling this method will raise a
            RuntimeError exception. If you want to save the WebMap object into a new web map item, call the `save()`
            method instead.

            For item_properties, pass in arguments for only the properties you want to be updated.
            All other properties will be untouched.  For example, if you want to update only the
            item's description, then only provide the description argument in item_properties.

        ===============     ====================================================================
        **Argument**        **Description**
        ---------------     --------------------------------------------------------------------
        item_properties     Optional dictionary. See table below for the keys and values.
        ---------------     --------------------------------------------------------------------
        thumbnail           Optional string. Either a path or URL to a thumbnail image.
        ---------------     --------------------------------------------------------------------
        metadata            Optional string. Either a path or URL to the metadata.
        ===============     ====================================================================

        *Key:Value Dictionary Options for Argument item_properties*

        =================  =====================================================================
        **Key**            **Value**
        -----------------  ---------------------------------------------------------------------
        typeKeywords       Optional string. Provide a lists all sub-types, see URL 1 below for valid values.
        -----------------  ---------------------------------------------------------------------
        description        Optional string. Description of the item.
        -----------------  ---------------------------------------------------------------------
        title              Optional string. Name label of the item.
        -----------------  ---------------------------------------------------------------------
        tags               Optional string. Tags listed as comma-separated values, or a list of strings.
                           Used for searches on items.
        -----------------  ---------------------------------------------------------------------
        snippet            Optional string. Provide a short summary (limit to max 250 characters) of the what the item is.
        -----------------  ---------------------------------------------------------------------
        accessInformation  Optional string. Information on the source of the content.
        -----------------  ---------------------------------------------------------------------
        licenseInfo        Optional string.  Any license information or restrictions regarding the content.
        -----------------  ---------------------------------------------------------------------
        culture            Optional string. Locale, country and language information.
        -----------------  ---------------------------------------------------------------------
        access             Optional string. Valid values are private, shared, org, or public.
        -----------------  ---------------------------------------------------------------------
        commentsEnabled    Optional boolean. Default is true, controls whether comments are allowed (true)
                           or not allowed (false).
        =================  =====================================================================

        URL 1: http://resources.arcgis.com/en/help/arcgis-rest-api/index.html#//02r3000000ms000000

        :return:
           A boolean indicating success (True) or failure (False).
        """

        if self.item is not None:
            item_properties['text'] = json.dumps(self._webmapdict, default=_date_handler)
            item_properties['extent'] = self._process_extent()
            if 'type' in item_properties:
                item_properties.pop('type')  # type should not be changed.
            return self.item.update({'text': json.dumps(self._webmapdict, default=_date_handler),
                                     'extent':self._process_extent()})
        else:
            raise RuntimeError('Item object missing, you should use `save()` method if you are creating a '
                               'new web map item')

    @property
    def layers(self):
        """
        Operational layers in the web map
        :return: List of Layer objects
        """
        if self._layers is not None:
            return self._layers
        else:
            self._layers = []
            if 'operationalLayers' in self._webmapdict.keys():
                for l in self._webmapdict['operationalLayers']:
                    self._layers.append(PropertyMap(l))

            #reverse the layer list - webmap viewer reverses the list always
            self._layers.reverse()
            return self._layers

    @property
    def basemap(self):
        """
        Base map layers in the web map
        :return: List of layer objects
        """
        if self._basemap:
            return PropertyMap(self._basemap)
        else:
            if "baseMap" in self._webmapdict.keys():
                self._basemap = self._webmapdict['baseMap']
            return PropertyMap(self._basemap)

    def remove_layer(self, layer):
        """
        Removes the specified layer from the web map. You can get the list of layers in map using the 'layers' property
        and pass one of those layers to this method for removal form the map.

        ==================     ====================================================================
        **Argument**           **Description**
        ------------------     --------------------------------------------------------------------
        layer                  Required object. Pass the layer that needs to be removed from the map. You can get the
                               list of layers in the map by calling the `layers` property.
        ==================     ====================================================================
        """

        self._webmapdict['operationalLayers'].remove(layer)
        self._layers.remove(PropertyMap(layer))

    @property
    def offline_areas(self):
        """
        Resource manager for offline areas cached for the web map
        :return:
        """
        return OfflineMapAreaManager(self.item, self._gis)


class OfflineMapAreaManager(object):
    """
    Helper class to manage offline map areas attached to a web map item. Users do not instantiate this class directly,
    instead, access the methods exposed by accessing the `offline_areas` property on the WebMap object.
    """
    def __init__(self, item, gis):
        self._gis = gis
        self._portal = gis._portal
        self._item = item

        # Get GP server url from helper services advertised by the GIS.
        try:
            self._url = self._gis.properties.helperServices.packaging.url
        except Exception:
            warn("GIS does not support creating packages for offline usage")

    def create(self, area, title=None, snippet=None, tags=None, folder_name=None):
        """
        Create offline map area items and packages for ArcGIS Runtime powered applications. This method creates two
        different types of items. It first creates 'Map Area' items for the specified extent or bookmark. Next it
        creates one or more map area packages corresponding to each layer type in the extent.

        .. note::
            - Offline map area functionality is only available if your GIS is ArcGIS Online.
            - There can be only 1 map area item for an extent or bookmark.
            - You need to be the owner of the web map or an administrator of your GIS.

        ==================     ====================================================================
        **Argument**           **Description**
        ------------------     --------------------------------------------------------------------
        area                   Required object. You can specify the name of a web map bookmark or a
                               desired extent.

                               To get the bookmarks from a web map, query the `definition.bookmarks`
                               property.

                               You can specify the extent as a list or dictionary of 'xmin', 'ymin',
                               'xmax', 'ymax' and spatial reference. If spatial reference is not
                               specified, it is assumed to be 'wkid' : 4326.
        ------------------     --------------------------------------------------------------------
        title                  Optional string. Specify a title for the output map area item.
        ------------------     --------------------------------------------------------------------
        snippet                Optional string. Specify a description for the output map area item.
        ------------------     --------------------------------------------------------------------
        tags                   Optional string or list of strings. Specify tags for output map area item.
        ------------------     --------------------------------------------------------------------
        folder_name            Optional string. Specify a folder name if you want the offline map area
                               item and the packages to be created inside a folder.
        ==================     ====================================================================

        .. note::
            This method executes silently. To view informative status messages, set the verbosity environment variable
            as shown below:

            .. code-block:: python

               USAGE EXAMPLE: setting verbosity

               from arcgis import env
               env.verbose = True

        :return:
            Item object for the offline map area item created.
        """
        # region find if bookmarks or extent is specified
        _bookmark = None
        _extent = None

        if isinstance(area, str):  # bookmark specified
            _bookmark = area
        elif isinstance(area, list):  # extent specified as list
            _extent = {'xmin': area[0][0],
                       'ymin': area[0][1],
                       'xmax': area[1][0],
                       'ymax': area[1][1],
                       'spatialReference':{'wkid': 4326}}

        elif isinstance(area, dict) and 'xmin' in area:  # geocoded extent provided
            _extent = area
            if 'spatialReference' not in _extent:
                _extent['spatialReference'] = {'wkid': 4326}
        # endregion

        # region build input parameters - for CreateMapArea tool
        if folder_name:
            user_folders = self._gis.users.me.folders
            if user_folders:
                matching_folder_ids = [f['id'] for f in user_folders if f['title'] == folder_name]
                if matching_folder_ids:
                    folder_id = matching_folder_ids[0]
                else:  # said folder not found in user account
                    folder_id = None
            else:  # ignore the folder, output will be created in same folder as web map
                folder_id = None
        else:
            folder_id = None

        if tags:
            if type(tags) is list:
                tags = ",".join('tags')

        output_name = {'title': title, 'snippet': snippet, 'tags': tags, 'folderId': folder_id}
        # endregion

        # region call CreateMapArea tool
        from arcgis.geoprocessing._tool import Toolbox
        pkg_tb = Toolbox(url=self._url, gis=self._gis)
        oma_result = pkg_tb.create_map_area(self._item.id, _bookmark, _extent, output_name=output_name)
        # endregion

        # region call the SetupMapArea tool
        setup_oma_result = pkg_tb.setup_map_area(oma_result)
        # print(setup_oma_result)
        _log.info(str(setup_oma_result))
        # endregion
        return Item(gis=self._gis, itemid=oma_result)

    def update(self, offline_map_area_items=None):
        """
        Refreshes existing map area packages associated with the list of map area items specified.
        This process updates the packages with changes made on the source data since the last time those packages were
        created or refreshed.

        .. note::
            - Offline map area functionality is only available if your GIS is ArcGIS Online.
            - You need to be the owner of the web map or an administrator of your GIS.

        ============================     ====================================================================
        **Argument**                     **Description**
        ----------------------------     --------------------------------------------------------------------
        offline_map_area_items           Optional list. Specify one or more Map Area items for which the packages need
                                         to be refreshed. If not specified, this method updates all the packages
                                         associated with all the map area items of the web map.

                                         To get the list of Map Area items related to the WebMap object, call the
                                         `list()` method.
        ============================     ====================================================================

        .. note::
            This method executes silently. To view informative status messages, set the verbosity environment variable
            as shown below:

            .. code-block:: python

               USAGE EXAMPLE: setting verbosity

               from arcgis import env
               env.verbose = True

        :return:
            Dictionary containing update status.
        """
        # find if 1 or a list of area items is provided
        if isinstance(offline_map_area_items, Item):
            offline_map_area_items = [offline_map_area_items]
        elif isinstance(offline_map_area_items, str):
            offline_map_area_items = [offline_map_area_items]

        # get packages related to the offline area item
        _related_packages = []
        if not offline_map_area_items:  # none specified
            _related_oma_items = self.list()
            for related_oma in _related_oma_items:  # get all offline packages for this web map
                _related_packages.extend(related_oma.related_items('Area2Package', 'forward'))

        else:
            for offline_map_area_item in offline_map_area_items:
                if isinstance(offline_map_area_item, Item):
                    _related_packages.extend(offline_map_area_item.related_items('Area2Package', 'forward'))
                elif isinstance(offline_map_area_item, str):
                    offline_map_area_item = Item(gis=self._gis, itemid=offline_map_area_item)
                    _related_packages.extend(offline_map_area_item.related_items('Area2Package', 'forward'))

        # update each of the packages
        if _related_packages:
            _update_list = [{'itemId': i.id} for i in _related_packages]

            # update the packages
            from arcgis.geoprocessing._tool import Toolbox
            pkg_tb = Toolbox(self._url, gis=self._gis)

            result = pkg_tb.refresh_map_area_package(json.dumps(_update_list,
                                                                default=_date_handler))
            return result
        else:
            return None

    def list(self):
        """
        Returns a list of Map Area items related to the current WebMap object.

        .. note::
            Map Area items and the corresponding offline packages cached for each share a relationship of type
            'Area2Package'. You can use this relationship to get the list of package items cached for a particular Map
            Area item. Refer to the Python snippet below for the steps:

            .. code-block:: python

               USAGE EXAMPLE: Finding packages cached for a Map Area item

               from arcgis.mapping import WebMap
               wm = WebMap(a_web_map_item_object)
               all_map_areas = wm.offline_areas.list()  # get all the offline areas for that web map

               area1 = all_map_areas[0]
               area1_packages = area1.related_items('Area2Package','forward')

               for pkg in area1_packages:
                    print(pkg.homepage)  # get the homepage url for each package item.

        :return:
        """

        _offline_areas = self._item.related_items('Map2Area', 'forward')
        return _offline_areas


class WebScene(collections.OrderedDict):
    """
    Represents a web scene and provides access to its basemaps and operational layers as well
    as functionality to visualize and interact with them.
    """

    def __init__(self, websceneitem):
        """
        Constructs a WebScene object given its item from ArcGIS Online or Portal.
        """
        if websceneitem.type.lower() != 'web scene':
            raise TypeError("item type must be web scene")
        self.item = websceneitem
        webscenedict = self.item.get_data()
        collections.OrderedDict.__init__(self, webscenedict)

    def _repr_html_(self):
        return '<iframe width=960 height=600 src="' + "https://www.arcgis.com/home/webscene/viewer.html?webscene=" + self.item.itemid + '"/>'

    def __repr__(self):
        return 'WebScene at ' + self.item._portal.url  + "/home/webscene/viewer.html?webscene=" + self.item.itemid

    def __str__(self):
        return json.dumps(self,
                          default=_date_handler)

    def update(self):
        # with _tempinput(self.__str__()) as tempfilename:
        self.item.update({'text': self.__str__()})


class VectorTileLayer(Layer):

    def __init__(self, url, gis=None):
        super(VectorTileLayer, self).__init__(url, gis)

    @classmethod
    def fromitem(cls, item):
        if not item.type == 'Vector Tile Service':
            raise TypeError("item must be a type of Vector Tile Service, not " + item.type)

        return cls(item.url, item._gis)

    @property
    def styles(self):
        url = "{url}/styles".format(url=self._url)
        params = {"f": "json"}
        return self._con.get(path=url, params=params, token=self._token)

    # ----------------------------------------------------------------------
    def tile_fonts(self, fontstack, stack_range):
        """This resource returns glyphs in PBF format. The template url for
        this fonts resource is represented in Vector Tile Style resource."""
        url = "{url}/resources/fonts/{fontstack}/{stack_range}.pbf".format(
            url=self._url,
            fontstack=fontstack,
            stack_range=stack_range)
        params = {}
        return self._con.get(path=url,
                             params=params, force_bytes=True, token=self._token)

    # ----------------------------------------------------------------------
    def vector_tile(self, level, row, column):
        """This resource represents a single vector tile for the map. The
        bytes for the tile at the specified level, row and column are
        returned in PBF format. If a tile is not found, an error is returned."""
        url = "{url}/tile/{level}/{row}/{column}.pbf".format(url=self._url,
                                                             level=level,
                                                             row=row,
                                                             column=column)
        params = {}
        return self._con.get(path=url,
                             params=params, try_json=False, force_bytes=True, token=self._token)

    # ----------------------------------------------------------------------
    def tile_sprite(self, out_format="sprite.json"):
        """
        This resource returns sprite image and metadata
        """
        url = "{url}/resources/sprites/{f}".format(url=self._url,
                                                   f=out_format)
        return self._con.get(path=url,
                             params={}, token=self._token)

    # ----------------------------------------------------------------------
    @property
    def info(self):
        """This returns relative paths to a list of resource files"""
        url = "{url}/resources/info".format(url=self._url)
        params = {"f": "json"}
        return self._con.get(path=url,
                             params=params, token=self._token)


class MapImageLayerManager(_GISResource):
    """ allows administration (if access permits) of ArcGIS Online hosted map image layers.
    A map image layer offers access to map and layer content.
    """

    def __init__(self, url, gis=None, map_img_lyr=None):
        super(MapImageLayerManager, self).__init__(url, gis)
        self._ms = map_img_lyr

    # ----------------------------------------------------------------------
    def refresh(self, service_definition=True):
        """
        The refresh operation refreshes a service, which clears the web
        server cache for the service.
        """
        url = self._url + "/MapServer/refresh"
        params = {
            "f": "json",
            "serviceDefinition": service_definition
        }

        res = self._con.post(self._url, params)

        super(MapImageLayerManager, self)._refresh()

        self._ms._refresh()

        return res

    # ----------------------------------------------------------------------
    def cancel_job(self, job_id):
        """
        The cancel job operation supports cancelling a job while update
        tiles is running from a hosted feature service. The result of this
        operation is a response indicating success or failure with error
        code and description.

        Inputs:
           job_id - job id to cancel
        """
        url = self._url + "/jobs/%s/cancel" % job_id
        params = {
            "f": "json"
        }
        return self._con.post(url, params)

    # ----------------------------------------------------------------------
    def job_statistics(self, job_id):
        """
        Returns the job statistics for the given jobId

        """
        url = self._url + "/jobs/%s" % job_id
        params = {
            "f": "json"
        }
        return self._con._post(url, params)
    #----------------------------------------------------------------------
    def update_tiles(self, levels=None, extent=None):
        """
        The starts tile generation for ArcGIS Online.  The levels of detail
        and the extent are provided to determine the area where tiles need
        to be rebuilt.


        ..Note: This operation is for ArcGIS Online only.

        ===============     ====================================================
        **Argument**        **Description**
        ---------------     ----------------------------------------------------
        levels              Optional string, The level of details to update
                            example: "1,2,10,20"
        ---------------     ----------------------------------------------------
        extent              Optional string, the area to update as Xmin, YMin, XMax, YMax
                            example: "-100,-50,200,500"
        ===============     ====================================================

        :returns:
           Dictionary. If the product is not ArcGIS Online tile service, the
           result will be None.
        """
        if self._gis._portal.is_arcgisonline:
            url = "%s/updateTiles" % self._url
            params = {
                "f" : "json"
            }
            if levels:
                params['levels'] = levels
            if extent:
                params['extent'] = extent
            return self._con.post(url, params)
        return None
    #----------------------------------------------------------------------
    @property
    def rerun_job(self, job_id, code):
        """
        The rerun job operation supports re-running a canceled job from a
        hosted map service. The result of this operation is a response
        indicating success or failure with error code and description.

        ===============     ====================================================
        **Argument**        **Description**
        ---------------     ----------------------------------------------------
        code                required string, parameter used to re-run a given
                            jobs with a specific error
                            code: ALL | ERROR | CANCELED
        ---------------     ----------------------------------------------------
        job_id              required string, job to reprocess
        ===============     ====================================================

        :returns:
           boolean or dictionary
        """
        url = self._url + "/jobs/%s/rerun" % job_id
        params = {
            "f" : "json",
            "rerun": code
        }
        return self._con._post(url, params)
    # ----------------------------------------------------------------------
    def edit_tile_service(self,
                          service_definition=None,
                          min_scale=None,
                          max_scale=None,
                          source_item_id=None,
                          export_tiles_allowed=False,
                          max_export_tile_count=100000):
        """
        This operation updates a Tile Service's properties

        Inputs:
           service_definition - updates a service definition
           min_scale - sets the services minimum scale for caching
           max_scale - sets the service's maximum scale for caching
           source_item_id - The Source Item ID is the GeoWarehouse Item ID of the map service
           export_tiles_allowed - sets the value to let users export tiles
           max_export_tile_count - sets the maximum amount of tiles to be exported
             from a single call.
        """
        params = {
            "f": "json",
        }
        if not service_definition is None:
            params["serviceDefinition"] = service_definition
        if not min_scale is None:
            params['minScale'] = float(min_scale)
        if not max_scale is None:
            params['maxScale'] = float(max_scale)
        if not source_item_id is None:
            params["sourceItemId"] = source_item_id
        if not export_tiles_allowed is None:
            params["exportTilesAllowed"] = export_tiles_allowed
        if not max_export_tile_count is None:
            params["maxExportTileCount"] = int(max_export_tile_count)
        url = self._url + "/edit"
        return self._con.post(url, params)
    #----------------------------------------------------------------------
    def delete_tiles(self, levels, extent=None ):
        """
        Deletes tiles for the current cache

        ===============     ====================================================
        **Argument**        **Description**
        ---------------     ----------------------------------------------------
        extent              optional dictionary,  If specified, the tiles within
                            this extent will be deleted or will be deleted based
                            on the service's full extent.
                            Example:
                            6224324.092137296,487347.5253569535,
                            11473407.698535524,4239488.369818687
                            the minx, miny, maxx, maxy values or,
                            {"xmin":6224324.092137296,"ymin":487347.5253569535,
                            "xmax":11473407.698535524,"ymax":4239488.369818687,
                            "spatialReference":{"wkid":102100}} the JSON
                            representation of the Extent object.
        ---------------     ----------------------------------------------------
        levels              required string, The level to delete.
                            Example, 0-5,10,11-20 or 1,2,3 or 0-5
        ===============     ====================================================

        :returns:
           dictionary
        """
        params = {
            "f" : "json",
            "levels" : levels,
        }
        if extent:
            params['extent'] = extent
        url = self._url + "/deleteTiles"
        return self._con.post(url, params)


class MapImageLayer(Layer):
    """
    MapImageLayer allows you to display and analyze data from sublayers defined in a map service, exporting images
    instead of features. Map service images are dynamically generated on the server based on a request, which includes
    an LOD (level of detail), a bounding box, dpi, spatial reference and other options. The exported image is of the
    entire map extent specified.

    MapImageLayer does not display tiled images. To display tiled map service layers, see TileLayer.
    """

    def __init__(self, url, gis=None):
        """
        .. Creates a map image layer given a URL. The URL will typically look like the following.

            https://<hostname>/arcgis/rest/services/<service-name>/MapServer

        :param url: the layer location
        :param gis: the GIS to which this layer belongs
        """
        super(MapImageLayer, self).__init__(url, gis)

        self._populate_layers()
        self._admin = None
        try:
            from arcgis.gis.server._service._adminfactory import AdminServiceGen
            self.service = AdminServiceGen(service=self, gis=gis)
        except: pass

    @classmethod
    def fromitem(cls, item):
        if not item.type == 'Map Service':
            raise TypeError("item must be a type of Map Service, not " + item.type)
        return cls(item.url, item._gis)

    def _populate_layers(self):
        layers = []
        tables = []

        for lyr in self.properties.layers:
            if 'subLayerIds' in lyr and lyr.subLayerIds is not None: # Group Layer
                lyr = Layer(self.url + '/' + str(lyr.id), self._gis)
            else:
                lyr = arcgis.features.FeatureLayer(self.url + '/' + str(lyr.id), self._gis, self)
            layers.append(lyr)

        for lyr in self.properties.tables:
            lyr = arcgis.features.Table(self.url + '/' + str(lyr.id), self._gis, self)
            tables.append(lyr)

        # fsurl = self.url + '/layers'
        # params = { "f" : "json" }
        # allayers = self._con.post(fsurl, params, token=self._token)

        # for layer in allayers['layers']:
        #    layers.append(FeatureLayer(self.url + '/' + str(layer['id']), self._gis))

        # for table in allayers['tables']:
        #    tables.append(FeatureLayer(self.url + '/' + str(table['id']), self._gis))

        self.layers = layers
        self.tables = tables

    @property
    def manager(self):
        if self._admin is None:
            """accesses the administration service"""
            url = self._url
            res = search("/rest/", url).span()
            addText = "admin/"
            part1 = url[:res[1]]
            part2 = url[res[1]:]
            adminURL = url.replace("/rest/", "/admin/").replace("/MapServer", ".MapServer")#"%s%s%s" % (part1, addText, part2)

            self._admin = MapImageLayerManager(adminURL, self._gis, self)
        return self._admin

    #----------------------------------------------------------------------
    def create_dynamic_layer(self, layer):
        """
        A dynamic layer / table method represents a single layer / table
        of a map service published by ArcGIS Server or of a registered
        workspace. This resource is supported only when the map image layer
        supports dynamic layers, as indicated by supportsDynamicLayers on
        the map image layer properties.

        =================     ====================================================================
        **Argument**          **Description**
        -----------------     --------------------------------------------------------------------
        layer                 required dict.  Dynamic layer/table source definition.
                              Syntax:
                              {
                                "id": <layerOrTableId>,
                                "source": <layer source>, //required
                                "definitionExpression": "<definitionExpression>",
                                "drawingInfo":
                                {
                                  "renderer": <renderer>,
                                  "transparency": <transparency>,
                                  "scaleSymbols": <true,false>,
                                  "showLabels": <true,false>,
                                  "labelingInfo": <labeling info>
                                },
                                "layerTimeOptions": //supported only for time enabled map layers
                                {
                                  "useTime" : <true,false>,
                                  "timeDataCumulative" : <true,false>,
                                  "timeOffset" : <timeOffset>,
                                  "timeOffsetUnits" : "<esriTimeUnitsCenturies,esriTimeUnitsDays,
                                                    esriTimeUnitsDecades,esriTimeUnitsHours,
                                                    esriTimeUnitsMilliseconds,esriTimeUnitsMinutes,
                                                    esriTimeUnitsMonths,esriTimeUnitsSeconds,
                                                    esriTimeUnitsWeeks,esriTimeUnitsYears |
                                                    esriTimeUnitsUnknown>"
                                }
                              }
        =================     ====================================================================

        :returns: arcgis.features.FeatureLayer or None (if not enabled)

        """
        if "supportsDynamicLayers" in self.properties and \
           self.properties["supportsDynamicLayers"]:
            from urllib.parse import urlencode
            url = "%s/dynamicLayer" % self._url
            d = urlencode(layer)
            url += "?layer=%s" % d
            return arcgis.features.FeatureLayer(url=url, gis=self._gis, dynamic_layer=layer)
        return None
    # ----------------------------------------------------------------------
    @property
    def kml(self):
        """returns the KML file for the layer"""
        url = "{url}/kml/mapImage.kmz".format(url=self._url)
        return self._con.get(url, {"f": 'json'},
                             file_name="mapImage.kmz",
                             out_folder=tempfile.gettempdir(), token=self._token)

    # ----------------------------------------------------------------------
    @property
    def item_info(self):
        """returns the service's item's infomation"""
        url = "{url}/info/iteminfo".format(url=self._url)
        params = {"f": "json"}
        return self._con.get(url, params, token=self._token)

    #----------------------------------------------------------------------
    @property
    def legend(self):
        """
        The legend resource represents a map service's legend. It returns
        the legend information for all layers in the service. Each layer's
        legend information includes the symbol images and labels for each
        symbol. Each symbol is an image of size 20 x 20 pixels at 96 DPI.
        Additional information for each layer such as the layer ID, name,
        and min and max scales are also included.

        The legend symbols include the base64 encoded imageData as well as
        a url that could be used to retrieve the image from the server.
        """
        url = "%s/legend" % self._url
        return self._con.get(path=url, params={'f': 'json'})

    # ----------------------------------------------------------------------
    @property
    def metadata(self):
        """returns the service's XML metadata file"""
        url = "{url}/info/metadata".format(url=self._url)
        params = {"f": "json"}
        return self._con.get(url, params, token=self._token)

    # ----------------------------------------------------------------------
    def thumbnail(self, out_path=None):
        """if present, this operation will download the image to local disk"""
        if out_path is None:
            out_path = tempfile.gettempdir()
        url = "{url}/info/thumbnail".format(url=self._url)
        params = {"f": "json"}
        if out_path is None:
            out_path = tempfile.gettempdir()
        return self._con.get(url,
                             params,
                             out_folder=out_path,
                             file_name="thumbnail.png", token=self._token)

    # ----------------------------------------------------------------------
    def identify(self,
                 geometry,
                 map_extent,
                 image_display,
                 geometry_type="Point",
                 sr=None,
                 layer_defs=None,
                 time_value=None,
                 time_options=None,
                 layers="all",
                 tolerance=None,
                 return_geometry=True,
                 max_offset=None,
                 precision=4,
                 dynamic_layers=None,
                 return_z=False,
                 return_m=False,
                 gdb_version=None,
                 return_unformatted=False,
                 return_field_name=False,
                 transformations=None,
                 map_range_values=None,
                 layer_range_values=None,
                 layer_parameters=None,
                 **kwargs):

        """
        The identify operation is performed on a map service resource
        to discover features at a geographic location. The result of this
        operation is an identify results resource. Each identified result
        includes its name, layer ID, layer name, geometry and geometry type,
        and other attributes of that result as name-value pairs.

        ==================     ====================================================================
        **Argument**           **Description**
        ------------------     --------------------------------------------------------------------
        geometry               required Geometry or list. The geometry to identify on. The type of
                               the geometry is specified by the geometryType parameter. The
                               structure of the geometries is same as the structure of the JSON
                               geometry objects returned by the API. In addition to the JSON
                               structures, for points and envelopes, you can specify the geometries
                               with a simpler comma-separated syntax.
        ------------------     --------------------------------------------------------------------
        geometry_type          required string.The type of geometry specified by the geometry
                               parameter. The geometry type could be a point, line, polygon, or an
                               envelope.
                               Values: Point,Multipoint,Polyline,Polygon,Envelope
        ------------------     --------------------------------------------------------------------
        sr                     optional dict, string, or SpatialReference. The well-known ID of the
                               spatial reference of the input and output geometries as well as the
                               map_extent. If sr is not specified, the geometry and the map_extent
                               are assumed to be in the spatial reference of the map, and the
                               output geometries are also in the spatial reference of the map.
        ------------------     --------------------------------------------------------------------
        layer_defs             optional dict. Allows you to filter the features of individual
                               layers in the exported map by specifying definition expressions for
                               those layers. Definition expression for a layer that is
                               published with the service will be always honored.
        ------------------     --------------------------------------------------------------------
        time_value             optional list. The time instant or the time extent of the features
                               to be identified.
        ------------------     --------------------------------------------------------------------
        time_options           optional dict. The time options per layer. Users can indicate
                               whether or not the layer should use the time extent specified by the
                               time parameter or not, whether to draw the layer features
                               cumulatively or not and the time offsets for the layer.
        ------------------     --------------------------------------------------------------------
        layers                 optional string. The layers to perform the identify operation on.
                               There are three ways to specify which layers to identify on:
                                - top: Only the top-most layer at the specified location.
                                - visible: All visible layers at the specified location.
                                - all: All layers at the specified location.
        ------------------     --------------------------------------------------------------------
        tolerance              optional integer. The distance in screen pixels from the specified
                               geometry within which the identify should be performed. The value for
                               the tolerance is an integer.
        ------------------     --------------------------------------------------------------------
        map_extent             required string. The extent or bounding box of the map currently
                               being viewed.
        ------------------     --------------------------------------------------------------------
        image_display          optional string. The screen image display parameters (width, height,
                               and DPI) of the map being currently viewed. The mapExtent and the
                               image_display parameters are used by the server to determine the
                               layers visible in the current extent. They are also used to
                               calculate the distance on the map to search based on the tolerance
                               in screen pixels.
                               Syntax: <width>, <height>, <dpi>
        ------------------     --------------------------------------------------------------------
        return_geometry        optional boolean. If true, the resultset will include the geometries
                               associated with each result. The default is true.
        ------------------     --------------------------------------------------------------------
        max_offset             optional integer. This option can be used to specify the maximum
                               allowable offset to be used for generalizing geometries returned by
                               the identify operation.
        ------------------     --------------------------------------------------------------------
        precision              optional integer. This option can be used to specify the number of
                               decimal places in the response geometries returned by the identify
                               operation. This applies to X and Y values only (not m or z-values).
        ------------------     --------------------------------------------------------------------
        dynamic_layers         optional dict. Use dynamicLayers property to reorder layers and
                               change the layer data source. dynamicLayers can also be used to add
                               new layer that was not defined in the map used to create the map
                               service. The new layer should have its source pointing to one of the
                               registered workspaces that was defined at the time the map service
                               was created.
                               The order of dynamicLayers array defines the layer drawing order.
                               The first element of the dynamicLayers is stacked on top of all
                               other layers. When defining a dynamic layer, source is required.
        ------------------     --------------------------------------------------------------------
        return_z               optional boolean. If true, Z values will be included in the results
                               if the features have Z values. Otherwise, Z values are not returned.
                               The default is false.
        ------------------     --------------------------------------------------------------------
        return_m               optional boolean.If true, M values will be included in the results
                               if the features have M values. Otherwise, M values are not returned.
                               The default is false.
        ------------------     --------------------------------------------------------------------
        gdb_version            optional string. Switch map layers to point to an alternate
                               geodatabase version.
        ------------------     --------------------------------------------------------------------
        return_unformatted     optional boolean. If true, the values in the result will not be
                               formatted i.e. numbers will returned as is and dates will be
                               returned as epoch values. The default is False.
        ------------------     --------------------------------------------------------------------
        return_field_name      optional boolean. Default is False. If true, field names will be
                               returned instead of field aliases.
        ------------------     --------------------------------------------------------------------
        transformations        optional list. Use this parameter to apply one or more datum
                               transformations to the map when sr is different than the map
                               service's spatial reference. It is an array of transformation
                               elements.
                               Transformations specified here are used to project features from
                               layers within a map service to sr.
        ------------------     --------------------------------------------------------------------
        map_range_values       optional list. Allows for the filtering features in the exported map
                               from all layer that are within the specified range instant or extent.
        ------------------     --------------------------------------------------------------------
        layer_range_values     optional list. Allows for the filtering of features for each
                               individual layer that are within the specified range instant or
                               extent.
        ------------------     --------------------------------------------------------------------
        layer_parameters       optional list. Allows for the filtering of the features of
                               individual layers in the exported map by specifying value(s) to an
                               array of pre-authored parameterized filters for those layers. When
                               value is not specified for any parameter in a request, the default
                               value, that is assigned during authoring time, gets used instead.
        =================     ====================================================================

        :returns: dictionary
        """

        if geometry_type.find("esriGeometry") == -1:
            geometry_type = "esriGeometry" + geometry_type
        if sr is None:
            sr = kwargs.pop('sr', None)
        if layer_defs is None:
            layer_defs = kwargs.pop('layerDefs', None)
        if time_value is None:
            time_value = kwargs.pop('layerTimeOptions', None)
        if return_geometry is None:
            return_geometry = kwargs.pop('returnGeometry', True)
        if return_m is None:
            return_m = kwargs.pop('returnM', False)
        if return_z is None:
            return_z = kwargs.pop('returnZ', False)
        if max_offset is None:
            max_offset = kwargs.pop('maxAllowableOffset', None)
        if precision is None:
            precision = kwargs.pop('geometryPrecision', None)
        if dynamic_layers is None:
            dynamic_layers = kwargs.pop('dynamicLayers', None)
        if gdb_version is None:
            gdb_version = kwargs.pop('gdbVersion', None)

        params = {'f': 'json',
                  'geometry': geometry,
                  'geometryType': geometry_type,
                  'tolerance': tolerance,
                  'mapExtent': map_extent,
                  'imageDisplay': image_display
                  }
        if sr:
            params['sr'] = sr
        if layer_defs:
            params['layerDefs'] = layer_defs
        if time_value:
            params['time'] = time_value
        if time_options:
            params['layerTimeOptions'] = time_options
        if layers:
            params['layers'] = layers
        if tolerance:
            params['tolerance'] = tolerance
        if return_geometry is not None:
            params['returnGeometry'] = return_geometry
        if max_offset:
            params['maxAllowableOffset'] = max_offset
        if precision:
            params['geometryPrecision'] = precision
        if dynamic_layers:
            params['dynamicLayers'] = dynamic_layers
        if return_m is not None:
            params['returnM'] = return_m
        if return_z is not None:
            params['returnZ'] = return_z
        if gdb_version:
            params['gdbVersion'] = gdb_version
        if return_unformatted is not None:
            params['returnUnformattedValues'] = return_unformatted
        if return_field_name is not None:
            params['returnFieldName'] = return_field_name
        if transformations:
            params['datumTransformations'] = transformations
        if map_range_values:
            params['mapRangeValues'] = map_range_values
        if layer_range_values:
            params['layerRangeValues'] = layer_range_values
        if layer_parameters:
            params['layerParameterValues'] = layer_parameters
        identifyURL = "{url}/identify".format(url=self._url)
        return self._con.post(identifyURL, params, token=self._token)

    # ----------------------------------------------------------------------
    def find(self,
             search_text,
             layers,
             contains=True,
             search_fields=None,
             sr=None,
             layer_defs=None,
             return_geometry=True,
             max_offset=None,
             precision=None,
             dynamic_layers=None,
             return_z=False,
             return_m=False,
             gdb_version=None,
             return_unformatted=False,
             return_field_name=False,
             transformations=None,
             map_range_values=None,
             layer_range_values=None,
             layer_parameters=None,
             **kwargs
             ):
        """
        performs the map service find operation

        ==================     ====================================================================
        **Argument**           **Description**
        ------------------     --------------------------------------------------------------------
        search_text            required string.The search string. This is the text that is searched
                               across the layers and fields the user specifies.
        ------------------     --------------------------------------------------------------------
        layers                 optional string. The layers to perform the identify operation on.
                               There are three ways to specify which layers to identify on:
                                - top: Only the top-most layer at the specified location.
                                - visible: All visible layers at the specified location.
                                - all: All layers at the specified location.
        ------------------     --------------------------------------------------------------------
        contains               optional boolean. If false, the operation searches for an exact
                               match of the search_text string. An exact match is case sensitive.
                               Otherwise, it searches for a value that contains the search_text
                               provided. This search is not case sensitive. The default is true.
        ------------------     --------------------------------------------------------------------
        search_fields          optional string. List of field names to look in.
        ------------------     --------------------------------------------------------------------
        sr                     optional dict, string, or SpatialReference. The well-known ID of the
                               spatial reference of the input and output geometries as well as the
                               map_extent. If sr is not specified, the geometry and the map_extent
                               are assumed to be in the spatial reference of the map, and the
                               output geometries are also in the spatial reference of the map.
        ------------------     --------------------------------------------------------------------
        layer_defs             optional dict. Allows you to filter the features of individual
                               layers in the exported map by specifying definition expressions for
                               those layers. Definition expression for a layer that is
                               published with the service will be always honored.
        ------------------     --------------------------------------------------------------------
        return_geometry        optional boolean. If true, the resultset will include the geometries
                               associated with each result. The default is true.
        ------------------     --------------------------------------------------------------------
        max_offset             optional integer. This option can be used to specify the maximum
                               allowable offset to be used for generalizing geometries returned by
                               the identify operation.
        ------------------     --------------------------------------------------------------------
        precision              optional integer. This option can be used to specify the number of
                               decimal places in the response geometries returned by the identify
                               operation. This applies to X and Y values only (not m or z-values).
        ------------------     --------------------------------------------------------------------
        dynamic_layers         optional dict. Use dynamicLayers property to reorder layers and
                               change the layer data source. dynamicLayers can also be used to add
                               new layer that was not defined in the map used to create the map
                               service. The new layer should have its source pointing to one of the
                               registered workspaces that was defined at the time the map service
                               was created.
                               The order of dynamicLayers array defines the layer drawing order.
                               The first element of the dynamicLayers is stacked on top of all
                               other layers. When defining a dynamic layer, source is required.
        ------------------     --------------------------------------------------------------------
        return_z               optional boolean. If true, Z values will be included in the results
                               if the features have Z values. Otherwise, Z values are not returned.
                               The default is false.
        ------------------     --------------------------------------------------------------------
        return_m               optional boolean.If true, M values will be included in the results
                               if the features have M values. Otherwise, M values are not returned.
                               The default is false.
        ------------------     --------------------------------------------------------------------
        gdb_version            optional string. Switch map layers to point to an alternate
                               geodatabase version.
        ------------------     --------------------------------------------------------------------
        return_unformatted     optional boolean. If true, the values in the result will not be
                               formatted i.e. numbers will returned as is and dates will be
                               returned as epoch values.
        ------------------     --------------------------------------------------------------------
        return_field_name      optional boolean. If true, field names will be returned instead of
                               field aliases.
        ------------------     --------------------------------------------------------------------
        transformations        optional list. Use this parameter to apply one or more datum
                               transformations to the map when sr is different than the map
                               service's spatial reference. It is an array of transformation
                               elements.
        ------------------     --------------------------------------------------------------------
        map_range_values       optional list. Allows you to filter features in the exported map
                               from all layer that are within the specified range instant or
                               extent.
        ------------------     --------------------------------------------------------------------
        layer_range_values     optional dictionary. Allows you to filter features for each
                               individual layer that are within the specified range instant or
                               extent. Note: Check range infos at the layer resources for the
                               available ranges.
        ------------------     --------------------------------------------------------------------
        layer_parameters       optional list. Allows you to filter the features of individual
                               layers in the exported map by specifying value(s) to an array of
                               pre-authored parameterized filters for those layers. When value is
                               not specified for any parameter in a request, the default value,
                               that is assigned during authoring time, gets used instead.
        ==================     ====================================================================

        :returns: dictionary
        """
        url = "{url}/find".format(url=self._url)
        params = {
            "f": "json",
            "searchText": search_text,
            "contains": contains,
        }
        if search_fields:
            params['searchFields'] = search_fields
        if sr:
            params['sr'] = sr
        if layer_defs:
            params['layerDefs'] = layer_defs
        if return_geometry is not None:
            params['returnGeometry'] = return_geometry
        if max_offset:
            params['maxAllowableOffset'] = max_offset
        if precision:
            params['geometryPrecision'] = precision
        if dynamic_layers:
            params['dynamicLayers'] = dynamic_layers
        if return_z is not None:
            params['returnZ'] = return_z
        if return_m is not None:
            params['returnM'] = return_m
        if gdb_version:
            params['gdbVersion'] = gdb_version
        if layers:
            params['layers'] = layers
        if return_unformatted is not None:
            params['returnUnformattedValues'] = return_unformatted
        if return_field_name is not None:
            params['returnFieldName'] = return_field_name
        if transformations:
            params['datumTransformations'] = transformations
        if map_range_values:
            params['mapRangeValues'] = map_range_values
        if layer_range_values:
            params['layerRangeValues'] = layer_range_values
        if layer_parameters:
            params['layerParameterValues'] = layer_parameters
        if len(kwargs) > 0:
            for k,v in kwargs.items():
                params[k] = v
        res = self._con.post(path=url,
                             postdata=params,
                             token=self._token)
        return res

    # ----------------------------------------------------------------------
    def generate_kml(self, save_location, name, layers, options="composite"):
        """
        The generateKml operation is performed on a map service resource.
        The result of this operation is a KML document wrapped in a KMZ
        file. The document contains a network link to the KML Service
        endpoint with properties and parameters you specify.

        =================     ====================================================================
        **Argument**          **Description**
        -----------------     --------------------------------------------------------------------
        save_location         required string. Save folder.
        -----------------     --------------------------------------------------------------------
        name                  The name of the resulting KML document. This is the name that
                              appears in the Places panel of Google Earth.
        -----------------     --------------------------------------------------------------------
        layers                required string. the layers to perform the generateKML operation on.
                              The layers are specified as a comma-separated list of layer ids.
        -----------------     --------------------------------------------------------------------
        options               required string. The layer drawing options. Based on the option
                              chosen, the layers are drawn as one composite image, as separate
                              images, or as vectors. When the KML capability is enabled, the
                              ArcGIS Server administrator has the option of setting the layer
                              operations allowed. If vectors are not allowed, then the caller will
                              not be able to get vectors. Instead, the caller receives a single
                              composite image.
                              values: composite, separateImage, nonComposite
        =================     ====================================================================

        :returns: string to file path

        """
        kmlURL = self._url + "/generateKml"
        params = {
            "f": "json",
            'docName': name,
            'layers': layers,
            'layerOptions': options
        }
        return self._con.get(kmlURL, params,
                             out_folder=save_location,
                             token=self._token)
    # ----------------------------------------------------------------------
    def export_map(self,
                   bbox,
                   bbox_sr=None,
                   size="600,550",
                   dpi=200,
                   image_sr=None,
                   image_format="png",
                   layer_defs=None,
                   layers=None,
                   transparent=False,
                   time_value=None,
                   time_options=None,
                   dynamic_layers=None,
                   gdb_version=None,
                   scale=None,
                   rotation=None,
                   transformation=None,
                   map_range_values=None,
                   layer_range_values=None,
                   layer_parameter=None,
                   f="json",
                   save_folder=None,
                   save_file=None,
                   **kwargs):
        """
        The export operation is performed on a map service resource.
        The result of this operation is a map image resource. This
        resource provides information about the exported map image such
        as its URL, its width and height, extent and scale.

        ==================     ====================================================================
        **Argument**          **Description**
        ------------------     --------------------------------------------------------------------
        bbox                   required string. The extent (bounding box) of the exported image.
                               Unless the bbox_sr parameter has been specified, the bbox is assumed
                               to be in the spatial reference of the map.
                               Example: bbox="-104,35.6,-94.32,41"
        ------------------     --------------------------------------------------------------------
        bbox_sr                optional integer, SpatialReference. spatial reference of the bbox.
        ------------------     --------------------------------------------------------------------
        size                   optional string. size - size of image in pixels
        ------------------     --------------------------------------------------------------------
        dpi                    optional integer. dots per inch
        ------------------     --------------------------------------------------------------------
        image_sr               optional integer, SpatialReference. spatial reference of the output
                               image
        ------------------     --------------------------------------------------------------------
        image_format           optional string. The format of the exported image.
                               The default format is .png.
                               Values: png | png8 | png24 | jpg | pdf | bmp | gif
                                       | svg | svgz | emf | ps | png32
        ------------------     --------------------------------------------------------------------
        layer_defs             optional dict. Allows you to filter the features of individual
                               layers in the exported map by specifying definition expressions for
                               those layers. Definition expression for a layer that is
                               published with the service will be always honored.
        ------------------     --------------------------------------------------------------------
        layers                 optional string. Determines which layers appear on the exported map.
                               There are four ways to specify which layers are shown:
                                 show: Only the layers specified in this list will
                                       be exported.
                                 hide: All layers except those specified in this
                                       list will be exported.
                                 include: In addition to the layers exported by
                                          default, the layers specified in this list
                                          will be exported.
                                 exclude: The layers exported by default excluding
                                          those specified in this list will be
                                          exported.
        ------------------     --------------------------------------------------------------------
        transparent            optional boolean. If true, the image will be exported with the
                               background color of the map set as its transparent color. The
                               default is false. Only the .png and .gif formats support
                               transparency.
        ------------------     --------------------------------------------------------------------
        time_value             optional list. The time instant or the time extent of the features
                               to be identified.
        ------------------     --------------------------------------------------------------------
        time_options           optional dict. The time options per layer. Users can indicate
                               whether or not the layer should use the time extent specified by the
                               time parameter or not, whether to draw the layer features
                               cumulatively or not and the time offsets for the layer.
        ------------------     --------------------------------------------------------------------
        dynamic_layers         optional dict. Use dynamicLayers property to reorder layers and
                               change the layer data source. dynamicLayers can also be used to add
                               new layer that was not defined in the map used to create the map
                               service. The new layer should have its source pointing to one of the
                               registered workspaces that was defined at the time the map service
                               was created.
                               The order of dynamicLayers array defines the layer drawing order.
                               The first element of the dynamicLayers is stacked on top of all
                               other layers. When defining a dynamic layer, source is required.
        ------------------     --------------------------------------------------------------------
        gdb_version            optional string. Switch map layers to point to an alternate
                               geodatabase version.
        ------------------     --------------------------------------------------------------------
        scale                  optional float. Use this parameter to export a map image at a
                               specific map scale, with the map centered around the center of the
                               specified bounding box (bbox)
        ------------------     --------------------------------------------------------------------
        rotation               optional float. Use this parameter to export a map image rotated at
                               a specific angle, with the map centered around the center of the
                               specified bounding box (bbox). It could be positive or negative
                               number.
        ------------------     --------------------------------------------------------------------
        transformations        optional list. Use this parameter to apply one or more datum
                               transformations to the map when sr is different than the map
                               service's spatial reference. It is an array of transformation
                               elements.
        ------------------     --------------------------------------------------------------------
        map_range_values       optional list. Allows you to filter features in the exported map
                               from all layer that are within the specified range instant or
                               extent.
        ------------------     --------------------------------------------------------------------
        layer_range_values     optional dictionary. Allows you to filter features for each
                               individual layer that are within the specified range instant or
                               extent. Note: Check range infos at the layer resources for the
                               available ranges.
        ------------------     --------------------------------------------------------------------
        layer_parameter        optional list. Allows you to filter the features of individual
                               layers in the exported map by specifying value(s) to an array of
                               pre-authored parameterized filters for those layers. When value is
                               not specified for any parameter in a request, the default value,
                               that is assigned during authoring time, gets used instead.
        ==================     ====================================================================

        :return: string, image of the map.
        """

        params = {
        }
        params["f"] = f
        params['bbox'] = bbox
        if bbox_sr:
            params['bboxSR'] = bbox_sr
        if dpi is not None:
            params['dpi'] = dpi
        if size is not None:
            params['size'] = size
        if image_sr is not None and \
           isinstance(image_sr, int):
            params['imageSR'] = {'wkid': image_sr}
        if image_format is not None:
            params['format'] = image_format
        if layer_defs is not None:
            params['layerDefs'] = layer_defs
        if layers is not None:
            params['layers'] = layers
        if transparent is not None:
            params['transparent'] = transparent
        if time_value is not None:
            params['time'] = time_value
        if time_options is not None:
            params['layerTimeOptions'] = time_options
        if dynamic_layers is not None:
            params['dynamicLayers'] = dynamic_layers
        if scale is not None:
            params['mapScale'] = scale
        if rotation is not None:
            params['rotation'] = rotation
        if gdb_version is not None:
            params['gdbVersion'] = gdb_version
        if transformation is not None:
            params['datumTransformations'] = transformation
        if map_range_values is not None:
            params['mapRangeValues'] = map_range_values
        if layer_range_values is not None:
            params['layerRangeValues'] = layer_range_values
        if layer_parameter:
            params['layerParameterValues'] = layer_parameter
        url = self._url + "/export"
        if len(kwargs) > 0:
            for k,v in kwargs.items():
                params[k] = v
        #return self._con.get(exportURL, params, token=self._token)

        if f == "json":
            return self._con.post(url, params, token=self._token)
        elif f == "image":
            if save_folder is not None and save_file is not None:
                return self._con.post(url, params,
                                      out_folder=save_folder, try_json=False,
                                      file_name=save_file, token=self._token)
            else:
                return self._con.post(url, params,
                                      try_json=False, force_bytes=True,
                                      token=self._token)
        elif f == "kmz":
            return self._con.post(url, params,
                                  out_folder=save_folder,
                                  file_name=save_file, token=self._token)
        else:
            print('Unsupported output format')

    # ----------------------------------------------------------------------
    def estimate_export_tiles_size(self,
                                   export_by,
                                   levels,
                                   tile_package=False,
                                   export_extent="DEFAULTEXTENT",
                                   area_of_interest=None,
                                   asynchronous=True,
                                   **kwargs):
        """
        The estimate_export_tiles_size method is an asynchronous task that
        allows estimation of the size of the tile package or the cache data
        set that you download using the Export Tiles operation. This
        operation can also be used to estimate the tile count in a tile
        package and determine if it will exceced the maxExportTileCount
        limit set by the administrator of the service. The result of this
        operation is Map Service Job. This job response contains reference
        to Map Service Result resource that returns the total size of the
        cache to be exported (in bytes) and the number of tiles that will
        be exported.

        ==================     ====================================================================
        **Argument**           **Description**
        ------------------     --------------------------------------------------------------------
        tile_package           optional boolean. Allows estimating the size for either a tile
                               package or a cache raster data set. Specify the value true for tile
                               packages format and false for Cache Raster data set. The default
                               value is False
        ------------------     --------------------------------------------------------------------
        levels                 required string. Specify the tiled service levels for which you want
                               to get the estimates. The values should correspond to Level IDs,
                               cache scales or the Resolution as specified in export_by parameter.
                               The values can be comma separated values or a range.
                               Example 1: 1,2,3,4,5,6,7,8,9
                               Example 2: 1-4,7-9
        ------------------     --------------------------------------------------------------------
        export_by              required string. The criteria that will be used to select the tile
                               service levels to export. The values can be Level IDs, cache scales
                               or the Resolution (in the case of image services).
                               Values: LevelID, Resolution, Scale
        ------------------     --------------------------------------------------------------------
        export_extent          The extent (bounding box) of the tile package or the cache dataset
                               to be exported. If extent does not include a spatial reference, the
                               extent values are assumed to be in the spatial reference of the map.
                               The default value is full extent of the tiled map service.
                               Syntax: <xmin>, <ymin>, <xmax>, <ymax>
                               Example: -104,35.6,-94.32,41
        ------------------     --------------------------------------------------------------------
        area_of_interest       optiona dictionary or Polygon. This allows exporting tiles within
                               the specified polygon areas. This parameter supersedes extent
                               parameter.
                               Example: { "features": [{"geometry":{"rings":[[[-100,35],
                                          [-100,45],[-90,45],[-90,35],[-100,35]]],
                                          "spatialReference":{"wkid":4326}}}]}
        ------------------     --------------------------------------------------------------------
        asynchronous           optional boolean. The estimate function is run asynchronously
                               requiring the tool status to be checked manually to force it to
                               run synchronously the tool will check the status until the
                               estimation completes.  The default is True, which means the status
                               of the job and results need to be checked manually.  If the value
                               is set to False, the function will wait until the task completes.
        ==================     ====================================================================

        :returns: dictionary

        """
        if self.properties['exportTilesAllowed'] == False:
            return
        import time
        url = self._url + "/estimateExportTilesSize"
        params = {
            "f": "json",
            "levels": levels,
            "exportBy": export_by,
            "tilePackage": tile_package,
            "exportExtent": export_extent
        }
        params["levels"] = levels
        if len(kwargs) > 0:
            for k,v in kwargs.items():
                params[k] = v
        if not area_of_interest is None:
            params['areaOfInterest'] = area_of_interest
        if asynchronous == True:
            return self._con.get(url, params, token=self._token)
        else:
            exportJob = self._con.get(url, params, token=self._token)

            job_id = exportJob['jobId']
            path = "%s/jobs/%s" % (url, exportJob['jobId'])

            params = {"f": "json"}
            job_response = self._con.post(path, params, token=self._token)

            if "status" in job_response:
                status = job_response.get("status")
                while not status == "esriJobSucceeded":
                    time.sleep(5)

                    job_response = self._con.post(path, params, token=self._token)
                    status = job_response.get("status")
                    if status in ['esriJobFailed',
                                  'esriJobCancelling',
                                  'esriJobCancelled',
                                  'esriJobTimedOut']:
                        print(str(job_response['messages']))
                        raise Exception('Job Failed with status ' + status)
            else:
                raise Exception("No job results.")

            return job_response['results']

    # ----------------------------------------------------------------------
    def export_tiles(self,
                     levels,
                     export_by="LevelID",
                     tile_package=False,
                     export_extent="DEFAULT",
                     optimize_for_size=True,
                     compression=75,
                     area_of_interest=None,
                     asynchronous=False,
                     **kwargs
                     ):
        """
        The exportTiles operation is performed as an asynchronous task and
        allows client applications to download map tiles from a server for
        offline use. This operation is performed on a Map Service that
        allows clients to export cache tiles. The result of this operation
        is Map Service Job. This job response contains a reference to the
        Map Service Result resource, which returns a URL to the resulting
        tile package (.tpk) or a cache raster dataset.
        exportTiles can be enabled in a service by using ArcGIS for Desktop
        or the ArcGIS Server Administrator Directory. In ArcGIS for Desktop
        make an admin or publisher connection to the server, go to service
        properties, and enable Allow Clients to Export Cache Tiles in the
        advanced caching page of the Service Editor. You can also specify
        the maximum tiles clients will be allowed to download. The default
        maximum allowed tile count is 100,000. To enable this capability
        using the Administrator Directory, edit the service, and set the
        properties exportTilesAllowed=true and maxExportTilesCount=100000.

        At 10.2.2 and later versions, exportTiles is supported as an
        operation of the Map Server. The use of the
        http://Map Service/exportTiles/submitJob operation is deprecated.
        You can provide arguments to the exportTiles operation as defined
        in the following parameters table:


        ==================     ====================================================================
        **Argument**           **Description**
        ------------------     --------------------------------------------------------------------
        levels                 required string. Specifies the tiled service levels to export. The
                               values should correspond to Level IDs, cache scales. or the
                               resolution as specified in export_by parameter. The values can be
                               comma separated values or a range. Make sure tiles are present at
                               the levels where you attempt to export tiles.
                               Example 1: 1,2,3,4,5,6,7,8,9
                               Example 2: 1-4,7-9
        ------------------     --------------------------------------------------------------------
        export_by              required string. The criteria that will be used to select the tile
                               service levels to export. The values can be Level IDs, cache scales.
                               or the resolution.  The defaut is 'LevelID'.
                               Values: LevelID | Resolution | Scale
        ------------------     --------------------------------------------------------------------
        tile_package           optiona boolean. Allows exporting either a tile package or a cache
                               raster data set. If the value is true, output will be in tile
                               package format, and if the value is false, a cache raster data
                               set is returned. The default value is false.
        ------------------     --------------------------------------------------------------------
        export_extent          optional dictionary or string. The extent (bounding box) of the tile
                               package or the cache dataset to be exported. If extent does not
                               include a spatial reference, the extent values are assumed to be in
                               the spatial reference of the map. The default value is full extent
                               of the tiled map service.
                               Syntax: <xmin>, <ymin>, <xmax>, <ymax>
                               Example 1: -104,35.6,-94.32,41
                               Example 2: {"xmin" : -109.55, "ymin" : 25.76,
                                            "xmax" : -86.39, "ymax" : 49.94,
                                            "spatialReference" : {"wkid" : 4326}}
        ------------------     --------------------------------------------------------------------
        optimize_for_size      optional boolean. Use this parameter to enable compression of JPEG
                               tiles and reduce the size of the downloaded tile package or the
                               cache raster data set. Compressing tiles slightly compromises the
                               quality of tiles but helps reduce the size of the download. Try
                               sample compressions to determine the optimal compression before
                               using this feature.
                               The default value is True.
        ------------------     --------------------------------------------------------------------
        compression=75,        optional integer. When optimize_for_size=true, you can specify a
                               compression factor. The value must be between 0 and 100. The value
                               cannot be greater than the default compression already set on the
                               original tile. For example, if the default value is 75, the value
                               of compressionQuality must be between 0 and 75. A value greater
                               than 75 in this example will attempt to up sample an already
                               compressed tile and will further degrade the quality of tiles.
        ------------------     --------------------------------------------------------------------
        area_of_interest       optional dictionary, Polygon. The area_of_interest polygon allows
                               exporting tiles within the specified polygon areas. This parameter
                               supersedes the exportExtent parameter.
                               Example: { "features": [{"geometry":{"rings":[[[-100,35],
                                                      [-100,45],[-90,45],[-90,35],[-100,35]]],
                                                      "spatialReference":{"wkid":4326}}}]}
        ------------------     --------------------------------------------------------------------
        asynchronous           optional boolean. Default False, this value ensures the returns are
                               returned to the user instead of the user having the check the job
                               status manually.
        ==================     ====================================================================

        :returns: path to download file is asynchronous is False. If True, a dictionary is returned.
        """
        import time
        params = {
            "f": "json",
            "tilePackage": tile_package,
            "exportExtent": export_extent,
            "optimizeTilesForSize": optimize_for_size,
            "compressionQuality": compression ,
            "exportBy": export_by,
            "levels": levels
        }
        if len(kwargs) > 0:
            for k,v in kwargs.items():
                params[k] = v
        url = self._url + "/exportTiles"
        if area_of_interest is not None:
            params["areaOfInterest"] = area_of_interest

        if asynchronous == True:
            return self._con.get(path=url, params=params, token=self._token)
        else:
            exportJob = self._con.get(path=url, params=params, token=self._token)

            job_id = exportJob['jobId']
            path = "%s/jobs/%s" % (url, exportJob['jobId'])

            params = {"f": "json"}
            job_response = self._con.post(path, params, token=self._token)

            if "status" in job_response:
                status = job_response.get("status")
                while not status == 'esriJobSucceeded':
                    time.sleep(5)

                    job_response = self._con.post(path, params, token=self._token)
                    status = job_response.get("status")
                    if status in ['esriJobFailed',
                                  'esriJobCancelling',
                                  'esriJobCancelled',
                                  'esriJobTimedOut']:
                        print(str(job_response['messages']))
                        raise Exception('Job Failed with status ' + status)
            else:
                raise Exception("No job results.")

            allResults = job_response['results']

            for k, v in allResults.items():
                if k == "out_service_url":
                    value = v.value
                    params = {
                        "f": "json"
                    }
                    gpRes = self._con.get(path=value, params=params, token=self._token)
                    if tile_package == True:
                        files = []
                        for f in gpRes['files']:
                            name = f['name']
                            dlURL = f['url']
                            files.append(
                                self._con.get(dlURL, params,
                                              out_folder=tempfile.gettempdir(),
                                              file_name=name), token=self._token)
                        return files
                    else:
                        return gpRes['folders']
                else:
                    return None
