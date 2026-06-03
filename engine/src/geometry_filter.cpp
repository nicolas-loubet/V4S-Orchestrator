#include <CppLibAmberGromacs.hpp>

namespace Filter {
    class GeometryConstraint {
        public:
            virtual ~GeometryConstraint()= default;
            virtual int isInside(const Vector& pos, const Vector& bounds) const= 0;
            virtual int getN() const { return 1; }
    };

    class BoxConstraint: public GeometryConstraint {
        Geometrics::Box box;

        public:
            BoxConstraint(const Geometrics::Box& b): box(b) {}
            int isInside(const Vector& pos, const Vector&) const override {
                return Geometrics::isInBox(pos,box);
            }
    };

    class CylinderConstraint: public GeometryConstraint {
        Geometrics::Line line;
        Real radius, hmin, hmax;

        public:
            CylinderConstraint(const Geometrics::Line& l, Real r, Real min, Real max): line(l), radius(r), hmin(min), hmax(max) {}

            int isInside(const Vector& pos, const Vector& bounds) const override {
                Real h= pos * line.dir.getNormalized();
                if(h < hmin || h > hmax) return false;
                return Geometrics::distanceToLine(line, pos, bounds) <= radius;
            }
    };

    class SphereConstraint: public GeometryConstraint {
        Vector center;
        Real radius;

        public:
            SphereConstraint(const Vector& c, Real r): center(c), radius(r) {}

            int isInside(const Vector& pos, const Vector& bounds) const override {
                return distancePBC(pos,center,bounds) <= radius;
            }
    };

    class MultiSphereConstraint: public GeometryConstraint {
        int N;
        vector<Vector> centers;
        Real radius;

        public:
            MultiSphereConstraint(const vector<Vector>& c, Real r): centers(c), radius(r) { N= centers.size(); }

            int getN() const override { return N; }

            int isInside(const Vector& pos, const Vector& bounds) const override {
                int output= 0;
                for(int i= 0; i < N; i++)
                    if(distancePBC(pos,centers[i],bounds) <= radius) {
                        output*= N;
                        output+= i+1;
                    }
                return output;
            }
    };
}
