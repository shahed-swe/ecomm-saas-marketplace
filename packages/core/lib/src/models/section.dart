/// A server-driven home section. The app renders what the tenant arranged in the page builder,
/// which is why a merchandising change needs no app release.
class Section {
  Section({required this.id, required this.type, required this.settings, this.data});

  factory Section.fromJson(Map<String, dynamic> json) => Section(
        id: json['id'] as String,
        type: json['type'] as String,
        settings: Map<String, dynamic>.from(json['settings'] as Map? ?? {}),
        data: json['data'] == null ? null : Map<String, dynamic>.from(json['data'] as Map),
      );

  final String id;
  final String type;
  final Map<String, dynamic> settings;
  final Map<String, dynamic>? data;

  String? get title => settings['title'] as String? ?? settings['heading'] as String?;
  List<dynamic> get items => (data?['items'] ?? data?['products'] ?? const []) as List<dynamic>;
}
